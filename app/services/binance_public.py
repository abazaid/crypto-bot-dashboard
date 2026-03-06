from typing import Dict, List

import requests

BASE_URL = "https://api.binance.com"
TIMEOUT = 12


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
