"""Shared pytest fixtures. No test here ever touches a real exchange."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Use an in-memory database for every test session before app modules import settings.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("BINANCE_API_KEY", "test-key")
os.environ.setdefault("BINANCE_API_SECRET", "test-secret")


@pytest.fixture()
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.database import Base
    from app.models import ai_pool  # noqa: F401  (register tables)

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class FakeExchange:
    """Minimal in-memory stand-in for app.services.binance_live used by the pool service."""

    def __init__(self, usdt_free: float = 1000.0, holdings: dict[str, float] | None = None, price: float = 100.0):
        self.usdt_free = usdt_free
        self.holdings: dict[str, float] = dict(holdings or {})
        self.price = price
        self.orders: list[dict] = []
        self.fail_next = False
        self.balances_down = False  # simulate get_balances() returning {} (API failure)
        self.ghost_fill: dict | None = None  # order that "filled" although the request raised
        self.filters = {"step_size": 0.001, "min_qty": 0.001, "min_notional": 5.0, "tick_size": 0.01}

    def is_configured(self) -> bool:
        return True

    def get_usdt_free(self) -> float:
        return self.usdt_free

    def get_order_by_client_id(self, symbol: str, client_order_id: str) -> dict | None:
        if self.ghost_fill and self.ghost_fill.get("cid") == client_order_id:
            return self.ghost_fill["raw"]
        return None

    def get_balances(self) -> dict[str, dict[str, float]]:
        if self.balances_down:
            return {}
        out = {"USDT": {"free": self.usdt_free, "locked": 0.0}}
        for asset, qty in self.holdings.items():
            out[asset] = {"free": qty, "locked": 0.0}
        return out

    def get_symbol_lot_filters(self, symbol: str) -> dict[str, float]:
        return dict(self.filters)

    def place_market_buy_quote(self, symbol: str, quote_usdt: float, client_order_id: str | None = None) -> dict:
        if self.fail_next == "ghost":
            # Request "times out" but the exchange actually filled it.
            self.fail_next = False
            qty = quote_usdt / self.price
            self.usdt_free -= quote_usdt
            base = symbol[:-4]
            self.holdings[base] = self.holdings.get(base, 0.0) + qty * 0.999
            self.ghost_fill = {"cid": client_order_id, "raw": {"orderId": 777, "status": "FILLED", "side": "BUY", "executedQty": qty, "cummulativeQuoteQty": quote_usdt}}
            raise RuntimeError("simulated timeout")
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated exchange failure")
        qty = quote_usdt / self.price
        fee_base = qty * 0.001
        self.usdt_free -= quote_usdt
        base = symbol[:-4]
        self.holdings[base] = self.holdings.get(base, 0.0) + qty - fee_base
        self.orders.append({"side": "BUY", "symbol": symbol, "quote": quote_usdt, "cid": client_order_id})
        return {
            "order_id": 1000 + len(self.orders),
            "status": "FILLED",
            "executed_qty": qty,
            "quote_qty": quote_usdt,
            "avg_price": self.price,
            "fee_base": fee_base,
            "fee_usdt": fee_base * self.price,
            "net_qty": qty - fee_base,
        }

    def place_market_sell_qty(self, symbol: str, quantity: float, client_order_id: str | None = None) -> dict:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated exchange failure")
        base = symbol[:-4]
        free = self.holdings.get(base, 0.0)
        qty = min(quantity, free)
        assert qty > 0, "sell qty must be positive"
        quote = qty * self.price
        fee = quote * 0.001
        self.holdings[base] = free - qty
        self.usdt_free += quote - fee
        self.orders.append({"side": "SELL", "symbol": symbol, "qty": qty, "cid": client_order_id})
        return {
            "order_id": 2000 + len(self.orders),
            "status": "FILLED",
            "executed_qty": qty,
            "quote_qty": quote,
            "avg_price": self.price,
            "fee_base": 0.0,
            "fee_usdt": fee,
            "net_qty": qty,
        }


@pytest.fixture()
def fake_exchange(monkeypatch):
    from app.services import ai_pool_service

    ex = FakeExchange()
    monkeypatch.setattr(ai_pool_service, "_exchange", lambda account: ex)
    monkeypatch.setattr(ai_pool_service, "_prices_for", lambda symbols: {s: ex.price for s in symbols})
    monkeypatch.setattr(ai_pool_service, "market_regime", lambda force_refresh=False: "bullish")
    return ex
