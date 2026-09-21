"""Ledger, isolation and exit-rule tests for the AI pool. Exchange is always faked."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.ai_pool import AiPool, AiPoolPosition, AiPoolTrade
from app.services import ai_pool_service as svc
from app.services.ai_strategy import Signal

pytestmark = pytest.mark.unit


def _signal(symbol: str = "ABCUSDT", price: float = 100.0, stop: float = 95.0, strategy: str = "breakout") -> Signal:
    return Signal(symbol, strategy, 80.0, price, 2.0, stop, price + 7.5, 0.4, 3.0, ["test"], {})


# ── Sizing (pure) ───────────────────────────────────────────────────────────────

def test_compute_entry_notional_risk_based():
    notional, why = svc.compute_entry_notional(equity=100.0, cash=100.0, risk_per_trade_pct=1.5, max_position_pct=35.0, stop_distance_pct=5.0, min_notional=6.0)
    assert why == "sized"
    assert notional == pytest.approx(30.0)  # 1.5 / 5%


def test_compute_entry_notional_capped_by_max_position():
    notional, _ = svc.compute_entry_notional(100.0, 100.0, 1.5, 35.0, 1.0, 6.0)
    assert notional == pytest.approx(35.0)


def test_compute_entry_notional_min_notional_rules():
    # small pool: sized 7.5 → fine
    n, why = svc.compute_entry_notional(50.0, 50.0, 1.5, 35.0, 10.0, 6.0)
    assert why == "sized" and n == pytest.approx(7.5)
    # tight risk budget: sized 3.33 < min 6, implied risk 0.9 <= 2*0.5 → use min
    n, why = svc.compute_entry_notional(50.0, 50.0, 1.0, 35.0, 15.0, 6.0)
    assert why == "min_notional" and n == 6.0
    # implied risk too high → skip
    n, why = svc.compute_entry_notional(20.0, 20.0, 0.5, 35.0, 30.0, 6.0)
    assert n == 0.0 and why == "min_notional_too_risky"
    # no cash
    n, why = svc.compute_entry_notional(50.0, 3.0, 1.5, 35.0, 5.0, 6.0)
    assert n == 0.0 and why == "cash_below_min_notional"


# ── Pool lifecycle & ledger ────────────────────────────────────────────────────

def test_create_pool_requires_account_balance(db_session, fake_exchange):
    fake_exchange.usdt_free = 20.0
    with pytest.raises(ValueError):
        svc.create_pool(db_session, 50.0)
    pool = svc.create_pool(db_session, 20.0)
    assert pool.cash_usdt == 20.0 and pool.allocated_usdt == 20.0
    with pytest.raises(ValueError):
        svc.create_pool(db_session, 10.0)  # only one pool per account


def test_buy_then_sell_updates_ledger_and_tags_orders(db_session, fake_exchange):
    pool = svc.create_pool(db_session, 100.0)
    pos = svc._buy(db_session, pool, _signal(), 30.0)
    db_session.commit()
    assert pos is not None
    assert pool.cash_usdt == pytest.approx(70.0)
    assert pos.qty == pytest.approx(0.3 * 0.999)
    assert fake_exchange.orders[-1]["cid"].startswith("AIPOOL-")

    fake_exchange.price = 110.0
    res = svc._sell(db_session, pool, pos, pos.qty, "manual", 110.0)
    db_session.commit()
    assert res is not None
    assert pos.status == "closed"
    # Position fully closed: realized pnl must equal what the pool actually has vs what it started with.
    assert pool.realized_pnl_usdt == pytest.approx(pool.cash_usdt - 100.0, abs=1e-6)
    assert pool.realized_pnl_usdt > 2.5  # ~ +10% on 30 USDT minus fees and step-rounding dust
    assert pos.invested_usdt == 0.0 and pos.qty == 0.0
    assert pool.trades_won == 1
    assert db_session.query(AiPoolTrade).count() == 2


def test_sell_never_exceeds_pool_qty_even_if_account_holds_more(db_session, fake_exchange):
    """Account already holds 5 ABC outside the pool; pool buys 0.3; sell must not touch the 5."""
    fake_exchange.holdings["ABC"] = 5.0
    pool = svc.create_pool(db_session, 100.0)
    pos = svc._buy(db_session, pool, _signal(), 30.0)
    db_session.commit()
    pool_qty = pos.qty
    svc._sell(db_session, pool, pos, 999.0, "manual", 100.0)
    db_session.commit()
    sold = fake_exchange.orders[-1]["qty"]
    assert sold <= pool_qty + 1e-9
    assert fake_exchange.holdings["ABC"] == pytest.approx(5.0 + pool_qty - sold, abs=1e-6)


def test_buy_refused_when_pool_cash_insufficient(db_session, fake_exchange):
    pool = svc.create_pool(db_session, 20.0)
    fake_exchange.usdt_free = 5000.0  # account is rich, pool is not
    pos = svc._buy(db_session, pool, _signal(), 25.0)
    assert pos is None
    assert pool.cash_usdt == 20.0
    assert not [o for o in fake_exchange.orders if o["side"] == "BUY"]


def test_withdraw_limited_to_cash(db_session, fake_exchange):
    pool = svc.create_pool(db_session, 50.0)
    svc._buy(db_session, pool, _signal(), 30.0)
    with pytest.raises(ValueError):
        svc.withdraw_funds(db_session, pool, 25.0)
    svc.withdraw_funds(db_session, pool, 10.0)
    assert pool.cash_usdt == pytest.approx(10.0)
    assert pool.allocated_usdt == pytest.approx(40.0)


# ── Exit management ────────────────────────────────────────────────────────────

def test_stop_loss_triggers_full_exit(db_session, fake_exchange):
    pool = svc.create_pool(db_session, 100.0)
    pos = svc._buy(db_session, pool, _signal(stop=95.0), 30.0)
    db_session.commit()
    svc._manage_position(db_session, pool, pos, 94.0, "bullish")
    db_session.commit()
    assert pos.status == "closed" and pos.close_reason == "stop"
    assert pool.trades_lost == 1
    assert pool.realized_pnl_usdt < 0


def test_tp1_partial_then_breakeven_and_trailing(db_session, fake_exchange):
    pool = svc.create_pool(db_session, 100.0)
    pos = svc._buy(db_session, pool, _signal(stop=95.0, strategy="breakout"), 30.0)
    db_session.commit()
    qty0 = pos.qty
    fake_exchange.price = 108.0
    svc._manage_position(db_session, pool, pos, 108.0, "bullish")  # tp1 at 107.5
    db_session.commit()
    assert pos.tp1_done is True
    assert pos.status == "open"
    assert pos.qty == pytest.approx(qty0 * 0.6, abs=0.001)  # lot-step rounding
    assert pos.stop_price >= 100.0  # breakeven
    # price runs → trailing ratchets up (highest 120 - 3*ATR(2) = 114)
    svc._manage_position(db_session, pool, pos, 120.0, "bullish")
    assert pos.stop_price == pytest.approx(114.0)
    # pullback below trail → exit remainder as 'trail'
    fake_exchange.price = 113.0
    svc._manage_position(db_session, pool, pos, 113.0, "bullish")
    db_session.commit()
    assert pos.status == "closed" and pos.close_reason == "trail"
    assert pool.realized_pnl_usdt > 0


def test_time_stop_exits_stale_position(db_session, fake_exchange):
    pool = svc.create_pool(db_session, 100.0)
    pos = svc._buy(db_session, pool, _signal(), 30.0)
    pos.opened_at = datetime.utcnow() - timedelta(hours=80)
    db_session.commit()
    svc._manage_position(db_session, pool, pos, 100.2, "bullish")
    db_session.commit()
    assert pos.status == "closed" and pos.close_reason == "time_stop"


# ── Reconciliation & breakers ──────────────────────────────────────────────────

def test_reconcile_detects_external_sale(db_session, fake_exchange):
    pool = svc.create_pool(db_session, 100.0)
    pos = svc._buy(db_session, pool, _signal(), 30.0)
    db_session.commit()
    fake_exchange.holdings["ABC"] = 0.0  # someone sold it from the All Coins page
    svc._reconcile(db_session, pool, [pos], {"ABCUSDT": 100.0})
    db_session.commit()
    assert pos.status == "closed" and pos.close_reason == "external"
    assert pos.qty == 0.0
    assert pool.realized_pnl_usdt == pytest.approx(-30.0)  # conservative write-off


def test_daily_loss_breaker_pauses_and_drawdown_halts(db_session, fake_exchange):
    pool = svc.create_pool(db_session, 100.0)
    svc._apply_breakers(db_session, pool, 95.5)  # -4.5% today
    assert pool.status == "paused" and pool.halt_reason.startswith("daily_loss")
    pool.status = "running"
    pool.halt_reason = None
    pool.day_start_equity_usdt = 84.0
    svc._apply_breakers(db_session, pool, 84.0)  # -16% from peak 100
    assert pool.status == "halted"


def test_consecutive_errors_pause_pool(db_session, fake_exchange):
    pool = svc.create_pool(db_session, 100.0)
    for _ in range(5):
        fake_exchange.fail_next = True
        assert svc._buy(db_session, pool, _signal(), 10.0) is None
    assert pool.status == "paused" and pool.halt_reason == "api_errors"
    assert pool.cash_usdt == 100.0


def test_manual_sell_fraction(db_session, fake_exchange):
    pool = svc.create_pool(db_session, 100.0)
    pos = svc._buy(db_session, pool, _signal(), 30.0)
    db_session.commit()
    qty0 = pos.qty
    res = svc.manual_sell_position(db_session, pos.id, 0.5)
    assert res["ok"]
    assert pos.qty == pytest.approx(qty0 * 0.5, abs=0.001)  # lot-step rounding
    assert pos.status == "open"


# ── Hardening regressions ──────────────────────────────────────────────────────

def test_balance_api_failure_does_not_write_off_position(db_session, fake_exchange):
    """get_balances() returning {} must be treated as an error, never as 'coin is gone'."""
    pool = svc.create_pool(db_session, 100.0)
    pos = svc._buy(db_session, pool, _signal(), 30.0)
    fake_exchange.balances_down = True
    res = svc._sell(db_session, pool, pos, pos.qty, "stop", 94.0)
    assert res is None
    assert pos.status == "open" and pos.qty > 0
    assert pool.realized_pnl_usdt == 0.0
    assert pool.consecutive_errors == 1
    fake_exchange.balances_down = False
    assert svc._sell(db_session, pool, pos, pos.qty, "stop", 94.0) is not None
    assert pos.status == "closed"


def test_buy_timeout_with_real_fill_is_recovered(db_session, fake_exchange):
    pool = svc.create_pool(db_session, 100.0)
    fake_exchange.fail_next = "ghost"
    pos = svc._buy(db_session, pool, _signal(), 30.0)
    assert pos is not None
    assert pool.cash_usdt == pytest.approx(70.0)
    assert pos.qty == pytest.approx(0.3 * 0.999, rel=1e-3)
    assert db_session.query(AiPoolTrade).filter(AiPoolTrade.side == "BUY").count() == 1


def test_each_order_is_committed_independently(db_session, fake_exchange):
    """A rollback after a successful order must not erase that order from the ledger."""
    pool = svc.create_pool(db_session, 100.0)
    pos = svc._buy(db_session, pool, _signal(), 30.0)
    db_session.rollback()
    db_session.expire_all()
    fresh = db_session.query(AiPoolPosition).filter(AiPoolPosition.id == pos.id).first()
    assert fresh is not None and fresh.status == "open"
    assert db_session.query(AiPool).first().cash_usdt == pytest.approx(70.0)


def test_nan_and_inf_amounts_are_rejected(db_session, fake_exchange):
    for bad in ("nan", "inf", "-inf", "abc"):
        with pytest.raises(ValueError):
            svc.create_pool(db_session, bad)
    pool = svc.create_pool(db_session, 50.0)
    for bad in ("nan", "inf"):
        with pytest.raises(ValueError):
            svc.add_funds(db_session, pool, bad)
        with pytest.raises(ValueError):
            svc.withdraw_funds(db_session, pool, bad)
    assert pool.cash_usdt == 50.0
    with pytest.raises(ValueError):
        svc.manual_sell_position(db_session, 1, 0)


def test_resume_after_daily_loss_resets_baseline(db_session, fake_exchange):
    pool = svc.create_pool(db_session, 100.0)
    pool.cash_usdt = 95.0  # simulate a 5% loss today
    svc._apply_breakers(db_session, pool, 95.0)
    assert pool.status == "paused"
    svc.resume_pool(db_session, pool)
    assert pool.status == "running"
    svc._apply_breakers(db_session, pool, 95.0)  # same equity: must NOT re-pause
    assert pool.status == "running"


def test_bnb_fee_is_part_of_cost_basis(db_session, fake_exchange, monkeypatch):
    pool = svc.create_pool(db_session, 100.0)
    real_buy = fake_exchange.place_market_buy_quote

    def buy_with_bnb_fee(symbol, quote, client_order_id=None):
        res = real_buy(symbol, quote, client_order_id)
        res["fee_base"] = 0.0
        res["net_qty"] = res["executed_qty"]
        res["fee_usdt"] = 0.03
        return res

    monkeypatch.setattr(fake_exchange, "place_market_buy_quote", buy_with_bnb_fee)
    pos = svc._buy(db_session, pool, _signal(), 30.0)
    assert pool.cash_usdt == pytest.approx(70.0 - 0.03)
    assert pos.invested_usdt == pytest.approx(30.03)


def test_error_rate_breaker_trips_despite_successes(db_session, fake_exchange):
    pool = svc.create_pool(db_session, 200.0)
    for i in range(12):
        fake_exchange.fail_next = True
        svc._buy(db_session, pool, _signal(symbol=f"C{i}USDT"), 6.0)
        if pool.status != "running":
            break
        pos = svc._buy(db_session, pool, _signal(symbol=f"OK{i}USDT"), 6.0)  # success resets the streak
        assert pos is not None
    assert pool.status == "paused" and pool.halt_reason == "api_error_rate"


def test_invalid_equity_halts_pool(db_session, fake_exchange):
    pool = svc.create_pool(db_session, 100.0)
    svc._apply_breakers(db_session, pool, float("nan"))
    assert pool.status == "halted" and pool.halt_reason == "invalid_equity"
