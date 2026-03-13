import hashlib
import hmac
import os
import time
from typing import Any
from urllib.parse import urlencode

import requests

BASE_URL = "https://api.binance.com"
RECV_WINDOW = 5000
TIMEOUT = 12

_exchange_info_cache: dict[str, Any] = {"ts": 0.0, "symbols": {}}


def is_configured() -> bool:
    return bool(os.getenv("BINANCE_API_KEY", "").strip() and os.getenv("BINANCE_API_SECRET", "").strip())


def _credentials() -> tuple[str, str]:
    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("Binance API credentials are missing. Set BINANCE_API_KEY and BINANCE_API_SECRET.")
    return api_key, api_secret


def _sign(query: str, api_secret: str) -> str:
    return hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


def _request(method: str, path: str, params: dict[str, Any] | None = None, signed: bool = False) -> Any:
    params = dict(params or {})
    api_key, api_secret = _credentials()
    headers = {"X-MBX-APIKEY": api_key}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = RECV_WINDOW
        query = urlencode(params, doseq=True)
        params["signature"] = _sign(query, api_secret)
    url = f"{BASE_URL}{path}"
    response = requests.request(method, url, params=params, headers=headers, timeout=TIMEOUT)
    if response.status_code >= 400:
        body = response.text.strip()
        raise RuntimeError(f"Binance error {response.status_code}: {body[:300]}")
    data = response.json()
    if isinstance(data, (dict, list)):
        return data
    raise RuntimeError("Unexpected Binance response format.")


def _get_exchange_symbols() -> dict[str, dict[str, Any]]:
    now = time.time()
    if _exchange_info_cache["symbols"] and now - float(_exchange_info_cache["ts"]) < 600:
        return _exchange_info_cache["symbols"]
    response = requests.get(f"{BASE_URL}/api/v3/exchangeInfo", timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    symbols: dict[str, dict[str, Any]] = {}
    for row in payload.get("symbols", []):
        s = row.get("symbol")
        if isinstance(s, str):
            symbols[s] = row
    _exchange_info_cache["ts"] = now
    _exchange_info_cache["symbols"] = symbols
    return symbols


def _round_step_down(value: float, step: float) -> float:
    if step <= 0:
        return value
    units = int(value / step)
    rounded = units * step
    precision = 0
    s = f"{step:.18f}".rstrip("0")
    if "." in s:
        precision = len(s.split(".")[1])
    return round(rounded, precision)


def _symbol_meta(symbol: str) -> tuple[str, float]:
    info = _get_exchange_symbols().get(symbol)
    if not info:
        raise RuntimeError(f"Symbol not found in Binance exchange info: {symbol}")
    base_asset = str(info.get("baseAsset", "")).upper()
    step_size = 0.0
    for f in info.get("filters", []):
        if f.get("filterType") == "LOT_SIZE":
            try:
                step_size = float(f.get("stepSize", "0"))
            except Exception:
                step_size = 0.0
            break
    return base_asset, step_size


def get_free_asset_balance(asset: str) -> float:
    account = _request("GET", "/api/v3/account", signed=True)
    for row in account.get("balances", []):
        if str(row.get("asset", "")).upper() == asset.upper():
            try:
                return float(row.get("free", "0"))
            except Exception:
                return 0.0
    return 0.0


def get_account_balances() -> list[dict[str, float | str]]:
    account = _request("GET", "/api/v3/account", signed=True)
    balances: list[dict[str, float | str]] = []
    for row in account.get("balances", []):
        asset = str(row.get("asset", "")).upper()
        try:
            free = float(row.get("free", "0") or 0.0)
        except Exception:
            free = 0.0
        try:
            locked = float(row.get("locked", "0") or 0.0)
        except Exception:
            locked = 0.0
        total = free + locked
        if asset and total > 0:
            balances.append({"asset": asset, "free": free, "locked": locked, "total": total})
    return balances


def get_my_trades(symbol: str, limit: int = 50) -> list[dict[str, Any]]:
    payload = _request(
        "GET",
        "/api/v3/myTrades",
        params={"symbol": symbol.upper(), "limit": max(1, min(limit, 1000))},
        signed=True,
    )
    if isinstance(payload, list):
        return payload
    raise RuntimeError("Unexpected Binance myTrades response format.")


def infer_manual_live_spot_buys(max_age_minutes: int = 180, min_notional_usdt: float = 10.0) -> list[dict[str, Any]]:
    stable_assets = {"USDT", "USDC", "FDUSD", "TUSD", "USDP", "DAI"}
    exchange_symbols = _get_exchange_symbols()
    now_ms = int(time.time() * 1000)
    inferred: list[dict[str, Any]] = []

    for balance in get_account_balances():
        asset = str(balance.get("asset", "")).upper()
        quantity = float(balance.get("total", 0.0) or 0.0)
        if asset in stable_assets or quantity <= 0:
            continue

        symbol = f"{asset}USDT"
        if symbol not in exchange_symbols:
            continue

        try:
            trades = get_my_trades(symbol, limit=50)
        except Exception:
            continue

        trades = sorted(trades, key=lambda row: int(row.get("time", 0) or 0), reverse=True)
        remaining = quantity
        accumulated_qty = 0.0
        accumulated_quote = 0.0
        newest_buy_time = 0
        newest_buy_order_id = "-"

        for trade in trades:
            is_buyer = bool(trade.get("isBuyer", False))
            if not is_buyer and accumulated_qty <= 0:
                break
            if not is_buyer:
                continue

            try:
                trade_qty = float(trade.get("qty", "0") or 0.0)
                trade_price = float(trade.get("price", "0") or 0.0)
            except Exception:
                continue
            if trade_qty <= 0 or trade_price <= 0:
                continue

            take_qty = min(remaining, trade_qty)
            accumulated_qty += take_qty
            accumulated_quote += take_qty * trade_price
            remaining -= take_qty

            trade_time = int(trade.get("time", 0) or 0)
            if trade_time > newest_buy_time:
                newest_buy_time = trade_time
                newest_buy_order_id = str(trade.get("orderId", "-"))

            if remaining <= max(quantity * 0.001, 1e-12):
                break

        if accumulated_qty <= 0:
            continue
        if remaining > max(quantity * 0.05, 1e-8):
            continue

        notional = accumulated_quote
        if notional < min_notional_usdt:
            continue

        age_minutes = (now_ms - newest_buy_time) / 60000 if newest_buy_time else 999999
        if max_age_minutes > 0 and age_minutes > max_age_minutes:
            continue

        inferred.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "entry_price": accumulated_quote / accumulated_qty,
                "order_id": newest_buy_order_id,
                "trade_time_ms": newest_buy_time,
                "notional_usdt": notional,
            }
        )

    return inferred


def place_market_buy_quote(symbol: str, quote_usdt: float) -> dict[str, Any]:
    if quote_usdt <= 0:
        raise RuntimeError("quote_usdt must be > 0")
    params = {
        "symbol": symbol.upper(),
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": f"{quote_usdt:.8f}",
        "newOrderRespType": "FULL",
    }
    return _request("POST", "/api/v3/order", params=params, signed=True)


def place_market_sell_qty(symbol: str, quantity: float) -> dict[str, Any]:
    if quantity <= 0:
        raise RuntimeError("quantity must be > 0")
    _, step = _symbol_meta(symbol.upper())
    qty = _round_step_down(quantity, step)
    if qty <= 0:
        raise RuntimeError("quantity became zero after step rounding")
    params = {
        "symbol": symbol.upper(),
        "side": "SELL",
        "type": "MARKET",
        "quantity": f"{qty:.8f}",
        "newOrderRespType": "FULL",
    }
    return _request("POST", "/api/v3/order", params=params, signed=True)


def get_base_asset(symbol: str) -> str:
    base_asset, _ = _symbol_meta(symbol.upper())
    return base_asset
