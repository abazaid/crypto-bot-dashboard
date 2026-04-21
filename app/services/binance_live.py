import hashlib
import hmac
import math
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import requests

from app.core.config import settings
from app.services.binance_public import get_book_tickers, get_exchange_info, get_prices

BASE_URL = "https://api.binance.com"
TIMEOUT = 15
TRADE_PAGE_LIMIT = 1000
MAX_TRADE_HISTORY_PAGES = 30
ACCOUNT_INFO_TTL_SECONDS = 5.0
COST_BASIS_TTL_SECONDS = 1800.0
_SYMBOL_FILTER_CACHE: dict[str, dict[str, float]] = {}
_CACHE_EXPIRES_AT = 0.0
_COST_BASIS_CACHE: dict[str, dict[str, Any]] = {}
_ALL_COINS_CACHE: dict[str, Any] = {"expires_at": 0.0, "key": "", "data": None}
_ACCOUNT_INFO_CACHE: dict[str, Any] = {"expires_at": 0.0, "data": None}
_COMPLETED_TRADES_CACHE: dict[str, Any] = {"expires_at": 0.0, "key": "", "data": None}
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
COMPLETED_TRADES_TTL_SECONDS = 120


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except Exception:
        return float(default)


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


def invalidate_account_cache() -> None:
    _ACCOUNT_INFO_CACHE["data"] = None
    _ACCOUNT_INFO_CACHE["expires_at"] = 0.0


def get_account_info() -> dict:
    now = time.time()
    cached = _ACCOUNT_INFO_CACHE.get("data")
    if cached is not None and float(_ACCOUNT_INFO_CACHE.get("expires_at", 0.0)) > now:
        return cached
    data = _signed_request("GET", "/api/v3/account")
    _ACCOUNT_INFO_CACHE["data"] = data
    _ACCOUNT_INFO_CACHE["expires_at"] = now + ACCOUNT_INFO_TTL_SECONDS
    return data


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
    invalidate_account_cache()
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
    invalidate_account_cache()
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
    invalidate_account_cache()
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
    invalidate_account_cache()
    return {
        "order_id": int(raw.get("orderId", 0)),
        "orig_qty": float(raw.get("origQty", 0.0)),
        "price": float(raw.get("price", 0.0)),
        "status": str(raw.get("status", "")),
    }


def cancel_order(symbol: str, order_id: int) -> dict:
    out = _signed_request(
        "DELETE",
        "/api/v3/order",
        {
            "symbol": symbol.upper(),
            "orderId": int(order_id),
        },
    )
    invalidate_account_cache()
    return out


def get_order(symbol: str, order_id: int) -> dict:
    return _signed_request(
        "GET",
        "/api/v3/order",
        {
            "symbol": symbol.upper(),
            "orderId": int(order_id),
        },
    )


def get_my_trades(symbol: str, limit: int = 1000, from_id: int | None = None) -> list[dict]:
    params: dict[str, Any] = {
        "symbol": symbol.upper(),
        "limit": max(1, min(int(limit), TRADE_PAGE_LIMIT)),
    }
    if from_id is not None and int(from_id) >= 0:
        params["fromId"] = int(from_id)
    out = _signed_request("GET", "/api/v3/myTrades", params)
    if isinstance(out, list):
        return out
    return []


def get_my_trades_full_history(symbol: str, max_pages: int = MAX_TRADE_HISTORY_PAGES) -> list[dict]:
    sym = symbol.upper()
    trades: list[dict] = []
    seen_ids: set[int] = set()
    next_from_id = 0

    for _ in range(max(1, int(max_pages))):
        batch = get_my_trades(sym, limit=TRADE_PAGE_LIMIT, from_id=next_from_id)
        if not batch:
            break

        batch_sorted = sorted(batch, key=lambda t: int(t.get("id", 0) or 0))
        added = 0
        last_trade_id = next_from_id
        for t in batch_sorted:
            trade_id = int(t.get("id", 0) or 0)
            last_trade_id = max(last_trade_id, trade_id)
            if trade_id in seen_ids:
                continue
            seen_ids.add(trade_id)
            trades.append(t)
            added += 1

        if len(batch_sorted) < TRADE_PAGE_LIMIT:
            break
        if added <= 0:
            break
        next_from_id = last_trade_id + 1

    return sorted(trades, key=lambda t: (int(t.get("time", 0) or 0), int(t.get("id", 0) or 0)))


def cancel_open_orders(symbol: str) -> list[dict]:
    out = _signed_request(
        "DELETE",
        "/api/v3/openOrders",
        {
            "symbol": symbol.upper(),
        },
    )
    invalidate_account_cache()
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


def _collect_candidate_trade_symbols(extra_symbols: list[str] | None = None) -> list[str]:
    _load_symbol_filters()
    candidates: set[str] = set()

    for asset, amounts in get_balances().items():
        total = _to_float(amounts.get("free", 0.0)) + _to_float(amounts.get("locked", 0.0))
        if total <= 0:
            continue
        if asset in {"USDT", "FDUSD", "USDC", "BUSD", "TUSD"}:
            continue
        pair = f"{str(asset).upper()}USDT"
        if pair in _SYMBOL_FILTER_CACHE:
            candidates.add(pair)

    try:
        for order in get_open_orders():
            sym = str(order.get("symbol", "")).upper()
            if sym:
                candidates.add(sym)
    except Exception:
        pass

    for sym in extra_symbols or []:
        normalized = str(sym or "").strip().upper()
        if not normalized:
            continue
        if normalized in _SYMBOL_FILTER_CACHE:
            candidates.add(normalized)

    return sorted(candidates)


def _fmt_hold_duration(seconds: float) -> str:
    sec = int(max(0, round(seconds)))
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {mins}m"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def get_completed_trades_from_binance(
    extra_symbols: list[str] | None = None,
    max_pages_per_symbol: int = 8,
    max_rows: int = 400,
) -> dict[str, Any]:
    symbols = _collect_candidate_trade_symbols(extra_symbols=extra_symbols)
    cache_key = "|".join(symbols)
    now_ts = time.time()
    if (
        _COMPLETED_TRADES_CACHE.get("data") is not None
        and _COMPLETED_TRADES_CACHE.get("key") == cache_key
        and float(_COMPLETED_TRADES_CACHE.get("expires_at", 0.0)) > now_ts
    ):
        return _COMPLETED_TRADES_CACHE["data"]

    rows: list[dict[str, Any]] = []
    symbols_with_trades: list[str] = []

    for symbol in symbols:
        try:
            trades = get_my_trades_full_history(symbol, max_pages=max_pages_per_symbol)
        except Exception:
            continue
        if not trades:
            continue

        symbols_with_trades.append(symbol)
        buy_lots: deque[dict[str, Any]] = deque()
        base_asset = _base_asset_from_symbol(symbol)

        for trade in trades:
            qty = _to_float(trade.get("qty", 0.0))
            quote_qty = _to_float(trade.get("quoteQty", 0.0))
            price = _to_float(trade.get("price", 0.0))
            trade_time_ms = int(trade.get("time", 0) or 0)
            trade_dt = datetime.fromtimestamp(trade_time_ms / 1000.0, tz=timezone.utc) if trade_time_ms > 0 else None
            is_buyer = bool(trade.get("isBuyer", False))

            commission = _to_float(trade.get("commission", 0.0))
            commission_asset = str(trade.get("commissionAsset", "")).upper()
            fee_usdt = _asset_to_usdt(commission_asset, commission, symbol, price)
            base_fee = commission if commission_asset == base_asset else 0.0

            if qty <= 0:
                continue

            if is_buyer:
                lot_qty = max(0.0, qty - base_fee)
                lot_cost = quote_qty + fee_usdt
                if lot_qty <= 0 or lot_cost <= 0:
                    continue
                buy_lots.append(
                    {
                        "qty_left": lot_qty,
                        "cost_left": lot_cost,
                        "buy_time": trade_dt,
                        "buy_price": price,
                    }
                )
                continue

            sell_qty = qty
            if sell_qty <= 0:
                continue

            sell_proceeds_net = max(0.0, quote_qty - fee_usdt)
            sell_time = trade_dt
            sell_price = price
            qty_to_match = sell_qty

            while qty_to_match > 1e-12 and buy_lots:
                lot = buy_lots[0]
                lot_qty_left = _to_float(lot.get("qty_left", 0.0))
                lot_cost_left = _to_float(lot.get("cost_left", 0.0))
                if lot_qty_left <= 1e-12:
                    buy_lots.popleft()
                    continue

                matched_qty = min(lot_qty_left, qty_to_match)
                cost_share = lot_cost_left * (matched_qty / lot_qty_left) if lot_qty_left > 0 else 0.0
                proceeds_share = sell_proceeds_net * (matched_qty / sell_qty) if sell_qty > 0 else 0.0
                pnl = proceeds_share - cost_share
                pnl_pct = (pnl / cost_share * 100.0) if cost_share > 0 else 0.0

                buy_time = lot.get("buy_time")
                hold_seconds = 0.0
                if isinstance(buy_time, datetime) and isinstance(sell_time, datetime):
                    hold_seconds = max(0.0, (sell_time - buy_time).total_seconds())

                rows.append(
                    {
                        "symbol": symbol,
                        "buy_time": buy_time,
                        "sell_time": sell_time,
                        "hold_seconds": hold_seconds,
                        "hold_text": _fmt_hold_duration(hold_seconds),
                        "buy_amount_usdt": cost_share,
                        "sell_amount_usdt": proceeds_share,
                        "buy_price": _to_float(lot.get("buy_price", 0.0)),
                        "sell_price": sell_price,
                        "pnl_usdt": pnl,
                        "pnl_pct": pnl_pct,
                    }
                )

                lot["qty_left"] = max(0.0, lot_qty_left - matched_qty)
                lot["cost_left"] = max(0.0, lot_cost_left - cost_share)
                qty_to_match = max(0.0, qty_to_match - matched_qty)

                if lot["qty_left"] <= 1e-12:
                    buy_lots.popleft()

    rows.sort(
        key=lambda r: (
            int(r["sell_time"].timestamp()) if isinstance(r.get("sell_time"), datetime) else 0,
            str(r.get("symbol", "")),
        ),
        reverse=True,
    )
    if max_rows > 0:
        rows = rows[: max(1, int(max_rows))]

    total_buy = sum(_to_float(r.get("buy_amount_usdt", 0.0)) for r in rows)
    total_sell = sum(_to_float(r.get("sell_amount_usdt", 0.0)) for r in rows)
    total_pnl = sum(_to_float(r.get("pnl_usdt", 0.0)) for r in rows)
    wins = sum(1 for r in rows if _to_float(r.get("pnl_usdt", 0.0)) > 0)
    losses = sum(1 for r in rows if _to_float(r.get("pnl_usdt", 0.0)) < 0)
    avg_hold_seconds = (sum(_to_float(r.get("hold_seconds", 0.0)) for r in rows) / len(rows)) if rows else 0.0

    out = {
        "rows": rows,
        "summary": {
            "total_trades": len(rows),
            "wins": wins,
            "losses": losses,
            "win_rate": ((wins / len(rows)) * 100.0) if rows else 0.0,
            "total_buy_usdt": total_buy,
            "total_sell_usdt": total_sell,
            "net_pnl_usdt": total_pnl,
            "net_pnl_pct": ((total_pnl / total_buy) * 100.0) if total_buy > 0 else 0.0,
            "avg_hold_text": _fmt_hold_duration(avg_hold_seconds),
        },
        "scanned_symbols": symbols,
        "symbols_with_trades": symbols_with_trades,
    }
    _COMPLETED_TRADES_CACHE["key"] = cache_key
    _COMPLETED_TRADES_CACHE["data"] = out
    _COMPLETED_TRADES_CACHE["expires_at"] = now_ts + float(COMPLETED_TRADES_TTL_SECONDS)
    return out


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
        if int(max_trades) > TRADE_PAGE_LIMIT:
            max_pages = max(1, math.ceil(float(max_trades) / float(TRADE_PAGE_LIMIT)))
            trades = get_my_trades_full_history(symbol, max_pages=min(max_pages, MAX_TRADE_HISTORY_PAGES))
        else:
            trades = get_my_trades(symbol, limit=max_trades)
    except Exception:
        return 0.0, 0.0, 0
    if not trades:
        return 0.0, 0.0, 0

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
            "expires_at": time.time() + COST_BASIS_TTL_SECONDS,
        }
        return 0.0, 0.0, used

    avg_entry = inv_cost / max(inv_qty, 1e-12)
    # Reconcile to current wallet qty after rebuilding from the deepest trade history we fetched.
    invested_now = avg_entry * max(qty_now, 0.0)
    _COST_BASIS_CACHE[symbol.upper()] = {
        "qty_now": float(qty_now),
        "avg_entry": float(max(avg_entry, 0.0)),
        "invested": float(max(invested_now, 0.0)),
        "used": int(used),
        "expires_at": time.time() + COST_BASIS_TTL_SECONDS,
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
    now = datetime.now(timezone.utc)

    # Filter to rows with sufficient market value before making trade history API calls.
    candidate_rows = []
    for r in symbol_rows:
        price = float(prices.get(r["symbol"], 0.0))
        market_value = price * float(r["qty_total"])
        if (not include_zero) and market_value < min_usdt_value:
            continue
        candidate_rows.append((r, price, market_value))

    # Fetch cost basis for all candidates in parallel (each makes one Binance API call).
    cost_basis: dict[str, tuple[float, float, int]] = {}
    with ThreadPoolExecutor(max_workers=min(len(candidate_rows), 10)) as ex:
        fut_to_sym = {
            ex.submit(
                _cost_basis_from_trades,
                r["symbol"],
                float(r["qty_total"]),
                TRADE_PAGE_LIMIT * MAX_TRADE_HISTORY_PAGES,
            ): r["symbol"]
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
