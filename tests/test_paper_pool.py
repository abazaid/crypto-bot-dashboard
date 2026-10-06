"""Paper AI Trader: simulated fills, ledger, and separation from the real account. No network."""
from __future__ import annotations

import pytest

from app.models.ai_pool import AiPoolTrade
from app.services import ai_pool_service as svc
from app.services import binance_live, paper_exchange
from app.services.ai_strategy import Signal

pytestmark = pytest.mark.unit

FILTERS = {"step_size": 0.001, "min_qty": 0.001, "min_notional": 5.0, "tick_size": 0.01}


@pytest.fixture()
def paper_market(monkeypatch):
    book = {"bid": 99.9, "ask": 100.1}
    monkeypatch.setattr(paper_exchange, "_book", lambda symbol: (book["bid"], book["ask"]))
    monkeypatch.setattr(paper_exchange, "get_symbol_lot_filters", lambda symbol: dict(FILTERS))

    def _no_real_orders(*args, **kwargs):
        raise AssertionError("paper pool must never place a real order")

    monkeypatch.setattr(binance_live, "place_market_buy_quote", _no_real_orders)
    monkeypatch.setattr(binance_live, "place_market_sell_qty", _no_real_orders)
    monkeypatch.setattr(svc, "_prices_for", lambda symbols: {s: book["bid"] for s in symbols})
    return book


def _signal(symbol: str = "ABCUSDT") -> Signal:
    return Signal(symbol, "breakout", 80.0, 100.0, 2.0, 95.0, 107.5, 0.4, 3.0, ["test"], {})


def test_paper_buy_fills_at_ask_with_slippage_and_base_fee(paper_market):
    res = paper_exchange.place_market_buy_quote("ABCUSDT", 50.0)
    price = 100.1 * (1 + paper_exchange.PAPER_SLIPPAGE_PCT / 100)
    assert res["avg_price"] == pytest.approx(price)
    assert res["executed_qty"] == pytest.approx(0.499, abs=1e-9)  # floored to step 0.001
    assert res["quote_qty"] <= 50.0
    assert res["net_qty"] == pytest.approx(res["executed_qty"] * (1 - 0.001))


def test_paper_sell_fills_at_bid_with_usdt_fee(paper_market):
    res = paper_exchange.place_market_sell_qty("ABCUSDT", 0.5)
    price = 99.9 * (1 - paper_exchange.PAPER_SLIPPAGE_PCT / 100)
    assert res["avg_price"] == pytest.approx(price)
    assert res["fee_base"] == 0.0
    assert res["fee_usdt"] == pytest.approx(0.5 * price * 0.001)


def test_paper_order_below_min_notional_is_rejected(paper_market):
    with pytest.raises(RuntimeError):
        paper_exchange.place_market_buy_quote("ABCUSDT", 3.0)


def test_paper_pool_needs_no_real_balance_and_is_separate(db_session, paper_market, monkeypatch):
    monkeypatch.setattr(binance_live, "get_balances", lambda: {"USDT": {"free": 0.0, "locked": 0.0}})
    pool = svc.create_pool(db_session, 5000.0, account=svc.PAPER_ACCOUNT, kind="paper", name="Paper")
    assert pool.cash_usdt == 5000.0 and pool.avoid_account_holdings is False
    assert svc.reserved_cash_usdt(db_session, "binance_1") == 0.0  # never counted against the real account
    with pytest.raises(ValueError):
        svc.create_pool(db_session, 100.0, account="binance_1", kind="paper")
    with pytest.raises(ValueError):
        svc.create_pool(db_session, 100.0, account=svc.PAPER_ACCOUNT, kind="ai")


def test_paper_round_trip_updates_ledger(db_session, paper_market):
    pool = svc.create_pool(db_session, 1000.0, account=svc.PAPER_ACCOUNT, kind="paper")
    pos = svc._buy(db_session, pool, _signal(), 100.0)
    assert pos is not None and pos.qty > 0
    assert pool.cash_usdt == pytest.approx(1000.0 - pos.invested_usdt)

    paper_market["bid"] = 110.0
    res = svc._sell(db_session, pool, pos, float(pos.qty), "manual", 110.0)
    assert res is not None and pos.status == "closed"
    assert pool.realized_pnl_usdt > 0
    assert pool.cash_usdt == pytest.approx(1000.0 + pool.realized_pnl_usdt)
    sides = [t.side for t in db_session.query(AiPoolTrade).filter(AiPoolTrade.pool_id == pool.id).all()]
    assert sides == ["BUY", "SELL"]


def test_reset_only_for_paper_pools(db_session, paper_market, fake_exchange):
    live = svc.create_pool(db_session, 50.0)
    with pytest.raises(ValueError):
        svc.reset_paper_pool(db_session, live)
    assert live.status == "running"
    paper = svc.create_pool(db_session, 500.0, account=svc.PAPER_ACCOUNT, kind="paper")
    svc.reset_paper_pool(db_session, paper)
    assert paper.status == "deleted"
    # a fresh paper test can start after a reset
    assert svc.create_pool(db_session, 800.0, account=svc.PAPER_ACCOUNT, kind="paper").cash_usdt == 800.0
