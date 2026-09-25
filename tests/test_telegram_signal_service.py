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
    pools.update_settings(db_session, pool, max_positions=5, entry_split_pct=0)  # split tested explicitly below
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
    # target 2 (0.187): sell 25% of initial (~6.3 USDT, above the minimum), stop -> T1 + 50% of the leg
    _price(fake_exchange, monkeypatch, 0.188)
    svc.manage_signal_position(db_session, tg_pool, pos, 0.188)
    assert pos.qty == pytest.approx(q0 - sold1 - q0 * 0.25, rel=0.03)
    assert pos.stop_price == pytest.approx(0.171 + 0.5 * (0.187 - 0.171))
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


def test_stop_exit_syncs_signal_status_via_close_hook(db_session, fake_exchange, tg_pool, monkeypatch):
    _price(fake_exchange, monkeypatch, 0.150)
    svc.ingest_message_db(db_session, "signal252", 1010, ALICE, datetime.utcnow())
    pos = db_session.query(AiPoolPosition).first()
    _price(fake_exchange, monkeypatch, 0.136)
    # production path: tick defers the exit and _execute_exits sells; the close hook must sync the row
    kind = pools._manage_position(db_session, tg_pool, pos, 0.136, "bullish", None, defer_full_exits=True)
    assert kind == "stop"
    pools._execute_exits(db_session, tg_pool, [(pos, kind, 0.136)], fake_exchange.get_balances())
    row = db_session.query(TelegramSignal).first()
    assert pos.status == "closed" and row.status == "closed" and "stop" in row.status_note


def test_plan_is_committed_with_the_position(db_session, fake_exchange, tg_pool, monkeypatch):
    _price(fake_exchange, monkeypatch, 0.150)
    svc.ingest_message_db(db_session, "signal252", 1011, ALICE, datetime.utcnow())
    db_session.rollback()
    db_session.expire_all()
    pos = db_session.query(AiPoolPosition).first()
    assert pos.signal_id is not None and json.loads(pos.plan_json)["targets"]


def test_far_price_rejects_misparsed_levels(db_session, fake_exchange, tg_pool, monkeypatch):
    _price(fake_exchange, monkeypatch, 150.0)  # 1000x the parsed zone
    res = svc.ingest_message_db(db_session, "signal252", 1012, ALICE, datetime.utcnow())
    assert res["status"] == "invalid" and "far from" in res["note"]
    assert not [o for o in fake_exchange.orders if o["side"] == "BUY"]


def test_gap_through_two_targets_fills_both_in_one_tick(db_session, fake_exchange, tg_pool, monkeypatch):
    _price(fake_exchange, monkeypatch, 0.150)
    svc.ingest_message_db(db_session, "signal252", 1013, ALICE, datetime.utcnow())
    pos = db_session.query(AiPoolPosition).first()
    _price(fake_exchange, monkeypatch, 0.190)  # clears 0.171 and 0.187 at once
    svc.manage_signal_position(db_session, tg_pool, pos, 0.190)
    plan = json.loads(pos.plan_json)
    assert plan["targets"][0]["done"] and plan["targets"][1]["done"] and not plan["targets"][2]["done"]
    assert pos.stop_price == pytest.approx(0.171 + 0.5 * (0.187 - 0.171))


def test_duplicate_race_hits_unique_constraint(db_session, fake_exchange, tg_pool, monkeypatch):
    _price(fake_exchange, monkeypatch, 0.150)
    svc.ingest_message_db(db_session, "signal252", 1014, ALICE, datetime.utcnow())
    db_session.add(TelegramSignal(channel="signal252", msg_id=1014, raw_text="x"))
    with pytest.raises(Exception):
        db_session.flush()
    db_session.rollback()


def test_lookup_failure_keeps_signal_pending(db_session, fake_exchange, tg_pool, monkeypatch):
    _price(fake_exchange, monkeypatch, 0.150)

    def boom(symbol):
        raise RuntimeError("timeout")

    monkeypatch.setattr(fake_exchange, "get_symbol_lot_filters", boom)
    res = svc.ingest_message_db(db_session, "signal252", 1015, ALICE, datetime.utcnow())
    assert res["status"] == "pending_entry"
    monkeypatch.setattr(fake_exchange, "get_symbol_lot_filters", lambda s: dict(fake_exchange.filters))
    assert svc.check_pending_signals(db_session, tg_pool) == 1


def test_last_target_keeps_a_runner_with_tight_giveback(db_session, fake_exchange, tg_pool, monkeypatch):
    tg_pool.cash_usdt = 300.0  # 100 USDT per slot so every slice clears the exchange minimum
    tg_pool.allocated_usdt = 300.0
    db_session.commit()
    _price(fake_exchange, monkeypatch, 0.150)
    svc.ingest_message_db(db_session, "signal252", 1020, ALICE, datetime.utcnow())
    pos = db_session.query(AiPoolPosition).first()
    q0 = pos.qty
    _price(fake_exchange, monkeypatch, 0.240)  # gaps through all four targets
    svc.manage_signal_position(db_session, tg_pool, pos, 0.240)
    assert pos.status == "open"
    # sold 20+25+25 + half of 30 = 85% -> 15% runner left
    assert pos.qty == pytest.approx(q0 * 0.15, rel=0.05)
    assert all(t["done"] for t in json.loads(pos.plan_json)["targets"])
    # runner: the higher of (T3 + 50% of the T3->T4 leg = 0.2215) and (peak give-back 30% -> 0.213)
    assert pos.stop_price == pytest.approx(0.210 + 0.5 * (0.233 - 0.210), rel=0.01)
    _price(fake_exchange, monkeypatch, 0.300)
    svc.manage_signal_position(db_session, tg_pool, pos, 0.300)
    assert pos.stop_price == pytest.approx(0.150 + 0.7 * (0.300 - 0.150), rel=0.01)  # ratchets up


def test_split_entry_half_now_half_at_zone_bottom(db_session, fake_exchange, tg_pool, monkeypatch):
    tg_pool.cash_usdt = 300.0
    tg_pool.allocated_usdt = 300.0
    tg_pool.entry_split_pct = 50.0
    db_session.commit()
    _price(fake_exchange, monkeypatch, 0.152)  # inside the zone, above the bottom (0.148)
    svc.ingest_message_db(db_session, "signal252", 1030, ALICE, datetime.utcnow())
    pos = db_session.query(AiPoolPosition).first()
    assert pos.invested_usdt == pytest.approx(30.0)  # half of the 60 USDT slot
    plan = json.loads(pos.plan_json)
    assert plan["leg2"]["amount"] == pytest.approx(30.0) and plan["leg2"]["price"] == 0.148
    q1 = pos.qty
    # price dips to the bottom -> second leg fills and the average entry drops
    _price(fake_exchange, monkeypatch, 0.148)
    svc.manage_signal_position(db_session, tg_pool, pos, 0.148)
    assert json.loads(pos.plan_json)["leg2"]["filled"] is True
    assert pos.qty > q1 * 1.9
    assert pos.avg_entry == pytest.approx((0.152 + 0.148) / 2, rel=0.01)
    assert pos.invested_usdt == pytest.approx(60.0)
    assert len([o for o in fake_exchange.orders if o["side"] == "BUY"]) == 2


def test_split_entry_cancelled_after_first_target(db_session, fake_exchange, tg_pool, monkeypatch):
    tg_pool.cash_usdt = 300.0
    tg_pool.allocated_usdt = 300.0
    tg_pool.entry_split_pct = 50.0
    db_session.commit()
    _price(fake_exchange, monkeypatch, 0.152)
    svc.ingest_message_db(db_session, "signal252", 1031, ALICE, datetime.utcnow())
    pos = db_session.query(AiPoolPosition).first()
    _price(fake_exchange, monkeypatch, 0.172)  # T1 first
    svc.manage_signal_position(db_session, tg_pool, pos, 0.172)
    leg2 = json.loads(pos.plan_json)["leg2"]
    assert leg2.get("cancelled") == "first target hit" and not leg2.get("filled")
    _price(fake_exchange, monkeypatch, 0.148)  # later dip: no second buy (the raised stop exits instead)
    svc.manage_signal_position(db_session, tg_pool, pos, 0.148)
    assert len([o for o in fake_exchange.orders if o["side"] == "BUY"]) == 1


def test_split_entry_buys_all_when_already_at_bottom(db_session, fake_exchange, tg_pool, monkeypatch):
    tg_pool.entry_split_pct = 50.0
    db_session.commit()
    _price(fake_exchange, monkeypatch, 0.147)  # below the zone bottom
    svc.ingest_message_db(db_session, "signal252", 1032, ALICE, datetime.utcnow())
    pos = db_session.query(AiPoolPosition).first()
    assert pos.invested_usdt == pytest.approx(20.0)
    assert "leg2" not in json.loads(pos.plan_json)
