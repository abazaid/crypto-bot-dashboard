import time
from typing import Dict, List

import requests

BASE_URL = "https://api.binance.com"
TIMEOUT = 12
_SYMBOL_CACHE: dict = {"expires_at": 0.0, "symbols": []}


def get_24h_tickers() -> List[dict]:
    r = requests.get(f"{BASE_URL}/api/v3/ticker/24hr", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_book_tickers() -> List[dict]:
    r = requests.get(f"{BASE_URL}/api/v3/ticker/bookTicker", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_klines(symbol: str, interval: str = "5m", limit: int = 250) -> List[list]:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(f"{BASE_URL}/api/v3/klines", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_prices(symbols: List[str]) -> Dict[str, float]:
    # Use real-time WebSocket cache when available
    try:
        from app.services.price_ws import get_cached_prices, is_connected
        if is_connected():
            cached = get_cached_prices(symbols)
            if len(cached) == len(symbols):
                return cached
    except Exception:
        pass

    # Fall back to REST API
    r = requests.get(f"{BASE_URL}/api/v3/ticker/price", timeout=TIMEOUT)
    r.raise_for_status()
    raw = r.json()
    wanted = set(symbols)
    prices: Dict[str, float] = {}
    for item in raw:
        sym = item["symbol"]
        if sym in wanted:
            prices[sym] = float(item["price"])
    return prices


def get_exchange_info() -> dict:
    r = requests.get(f"{BASE_URL}/api/v3/exchangeInfo", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def list_tradable_usdt_symbols() -> List[str]:
    now = time.time()
    if _SYMBOL_CACHE["expires_at"] > now and _SYMBOL_CACHE["symbols"]:
        return list(_SYMBOL_CACHE["symbols"])

    data = get_exchange_info()
    symbols: List[str] = []
    for row in data.get("symbols", []):
        if row.get("status") != "TRADING":
            continue
        if row.get("quoteAsset") != "USDT":
            continue
        symbols.append(str(row.get("symbol", "")).upper())
    symbols = sorted(set(symbols))

    _SYMBOL_CACHE["symbols"] = symbols
    _SYMBOL_CACHE["expires_at"] = now + 900
    return list(symbols)


def search_symbols(query: str, limit: int = 30) -> List[str]:
    q = (query or "").upper().strip()
    if not q:
        return list_tradable_usdt_symbols()[:limit]
    out: List[str] = []
    for symbol in list_tradable_usdt_symbols():
        if q in symbol:
            out.append(symbol)
        if len(out) >= limit:
            break
    return out
