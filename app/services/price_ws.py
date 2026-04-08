"""
Real-time price cache via Binance WebSocket.

Subscribes to !miniTicker@arr (all symbols, updated ~1s).
APScheduler jobs read from _live_prices instead of hitting REST every cycle.
Falls back to REST API if WebSocket not yet connected.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

_live_prices: Dict[str, float] = {}
_connected: bool = False


def get_cached_prices(symbols: List[str]) -> Dict[str, float]:
    """Return latest cached prices for the requested symbols."""
    return {s: _live_prices[s] for s in symbols if s in _live_prices}


def is_connected() -> bool:
    return _connected


def all_cached_prices() -> Dict[str, float]:
    return dict(_live_prices)


async def run_price_stream() -> None:
    """Asyncio task — runs forever, reconnects on disconnect."""
    global _connected
    import websockets  # lazy import so startup doesn't fail if missing

    url = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                _connected = True
                logger.info("PriceWS: connected to Binance stream")
                async for raw in ws:
                    tickers = json.loads(raw)
                    for t in tickers:
                        _live_prices[t["s"]] = float(t["c"])
        except Exception as exc:
            _connected = False
            logger.warning("PriceWS: disconnected (%s) — reconnecting in 5 s", exc)
            await asyncio.sleep(5)
