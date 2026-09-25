"""End-to-end (fake exchange) tests for the Telegram signal follower."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.models.ai_pool import AiPoolPosition, TelegramSignal
from app.services import ai_pool_service as pools
from app.services import telegram_signal_service as svc
from test_telegram_signals import ALICE

pytestmark = pytest.mark.unit


@pytest.fixture()
def tg_pool(db_session, fake_exchange):
    pool = pools.create_pool(db_session, 100.0, kind="telegram", name="Signals")
    pools.update_settings(db_session, pool, max_positions=5)
    return pool


def _price(fake_exchange, monkeypatch, px: float):
    fake_exchange.price = px
    monkeypatch.setattr(pools, "_prices_for", lambda symbols: {s: px for s in symbols})


def test_signal_inside_zone_is_entered_with_slot_sizing(db_session, fake_exchange, tg_pool, monkeypatch):
    _price(fake_exchange, monkeypatch, 0.150)
    res = svc.ingest_message_db(db_session, "signal252", 1001, ALICE, datetime.utcnow())
    assert res["status"] == "entered"
    row = db_session.query(TelegramSignal).first()
    pos = db_session.query(AiPoolPosition).filter(AiPoolPosition.id == row.position_id).first()
    assert pos is not None and pos.strategy == "telegram"
    assert pos.invested_usdt == pytest.approx(20.0)  # 100 / 5 slots
    assert pos.stop_price == pytest.approx(0.137)
    plan = json.loads(pos.plan_json)
    assert [t["price"] for t in plan["targets"]] == [0.171, 0.187, 0.210, 0.233]
    assert svc.get_last_msg_id(db_session, "signal252") == 1001


def test_duplicate_and_edit_are_ignored(db_session, fake_exchange, tg_pool, monkeypatch):
    _price(fake_exchange, monkeypatch, 0.150)
    svc.ingest_message_db(db_session, "signal252", 1001, ALICE, datetime.utcnow())
    res = svc.ingest_message_db(db_session, "signal252", 1001, ALICE + "\n✅", datetime.utcnow())
    assert "duplicate" in res["note"]
    assert db_session.query(AiPoolPosition).count() == 1


def test_above_zone_waits_then_enters_or_expires(db_session, fake_exchange, tg_pool, monkeypatch):
    _price(fake_exchange, monkeypatch, 0.165)  # above 0.1556
    res = svc.ingest_message_db(db_session, "signal252", 1002, ALICE, datetime.utcnow())
    assert res["status"] == "pending_entry"
    assert db_session.query(AiPoolPosition).count() == 0
    _price(fake_exchange, monkeypatch, 0.152)
    assert svc.check_pending_signals(db_session, tg_pool) == 1
    row = db_session.query(TelegramSignal).first()
    assert row.status == "entered"
    # a second pending signal that expires
    _price(fake_exchange, monkeypatch, 0.170)
    svc.ingest_message_db(db_session, "signal252", 1003, ALICE.replace("#ALICE", "#BOB"), datetime.utcnow())
    row2 = db_session.query(TelegramSignal).filter(TelegramSignal.msg_id == 1003).first()
    row2.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db_session.commit()
    svc.check_pending_signals(db_session, tg_pool)
    assert row2.status == "missed"


def test_no_cash_is_skipped_not_forced(db_session, fake_exchange, tg_pool, monkeypatch):
    tg_pool.cash_usdt = 3.0
    db_session.commit()
    _price(fake_exchange, monkeypatch, 0.150)
    res = svc.ingest_message_db(db_session, "signal252", 1004, ALICE, datetime.utcnow())
    assert res["status"] == "skipped_cash"
    assert not [o for o in fake_exchange.orders if o["side"] == "BUY"]


def test_other_exchange_and_chatter(db_session, fake_exchange, tg_pool, monkeypatch):
    _price(fake_exchange, monkeypatch, 0.150)
    assert svc.ingest_message_db(db_session, "signal252", 1005, "صباح الخير", datetime.utcnow()) is None
    res = svc.ingest_message_db(db_session, "signal252", 1006, ALICE.replace("| Binance", "| Bybit"), datetime.utcnow())
    assert res["status"] == "not_binance"


def test_target_ladder_sells_fractions_and_steps_stop_behind(db_session, fake_exchange, tg_pool, monkeypatch):
    _price(fake_exchange, monkeypatch, 0.150)
    svc.ingest_message_db(db_session, "signal252", 1007, ALICE, datetime.utcnow())
    pos = db_session.query(AiPoolPosition).first()
    q0 = pos.qty
    # target 1 (0.171): the channel's 20% slice (~4.6 USDT) is under Binance's 5 USDT minimum,
    # so the engine sells the minimum (5.5 USDT worth) instead; stop -> breakeven
    _price(fake_exchange, monkeypatch, 0.172)
    svc.manage_signal_position(db_session, tg_pool, pos, 0.172)
    sold1 = max(q0 * 0.2, 5.5 / 0.172)
    assert pos.qty == pytest.approx(q0 - sold1, rel=0.02)
    assert pos.stop_price >= 0.150
    # target 2 (0.187): sell 25% of initial (~6.3 USDT, above the minimum), stop -> target 1
    _price(fake_exchange, monkeypatch, 0.188)
    svc.manage_signal_position(db_session, tg_pool, pos, 0.188)
    assert pos.qty == pytest.approx(q0 - sold1 - q0 * 0.25, rel=0.03)
    assert pos.stop_price == pytest.approx(0.171)
    # pullback to 0.170 -> stopped out as 'trail' with profit
    _price(fake_exchange, monkeypatch, 0.170)
    kind = svc.manage_signal_position(db_session, tg_pool, pos, 0.170, defer_full_exits=True)
    assert kind == "trail"
    svc.manage_signal_position(db_session, tg_pool, pos, 0.170)
    assert pos.status == "closed" and pos.realized_pnl_usdt > 0
    row = db_session.query(TelegramSignal).first()
    assert row.status == "closed"


def test_hard_stop_before_any_target(db_session, fake_exchange, tg_pool, monkeypatch):
    _price(fake_exchange, monkeypatch, 0.150)
    svc.ingest_message_db(db_session, "signal252", 1008, ALICE, datetime.utcnow())
    pos = db_session.query(AiPoolPosition).first()
    _price(fake_exchange, monkeypatch, 0.136)
    svc.manage_signal_position(db_session, tg_pool, pos, 0.136)
    assert pos.status == "closed" and pos.close_reason == "stop"
    assert tg_pool.trades_lost == 1


def test_ai_and_telegram_pools_coexist_and_tick_routes_exits(db_session, fake_exchange, tg_pool, monkeypatch):
    ai = pools.create_pool(db_session, 50.0, kind="ai", name="AI")
    assert ai.id != tg_pool.id
    _price(fake_exchange, monkeypatch, 0.150)
    svc.ingest_message_db(db_session, "signal252", 1009, ALICE, datetime.utcnow())
    pos = db_session.query(AiPoolPosition).first()
    # the generic manager must dispatch telegram positions to the signal ladder
    _price(fake_exchange, monkeypatch, 0.172)
    pools._manage_position(db_session, tg_pool, pos, 0.172, "bullish")
    assert json.loads(pos.plan_json)["targets"][0]["done"] is True
