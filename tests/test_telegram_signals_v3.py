"""Parser tests for the "Shaban vip" bot-style posts (spot only), using real channel posts."""
from __future__ import annotations

import asyncio

import pytest

from app.services import telegram_listener as tl
from app.services.telegram_signals import looks_like_any_signal, parse_any, validate_signal
from app.services.telegram_signals_v3 import is_futures, parse_signal_v3

pytestmark = pytest.mark.unit

VIRTUAL = """🔵 VIRTUAL/USDT 🤖📈
📍 Exchange: OKX

⚪️ Buy Zone: 0.7786 – 0.7500

🟡 Targets: 🎯
🟢 T1: 0.7919
🟢 T2: 0.8175
🟢 T3: 0.8408
🟢 T4: 0.9343
🟢 T5: 1.0121

🔴 Stop Loss: 0.6827  اغلاق يوم

✅ ملاحظة: الالتزام بمنطقة الدخول وإدارة رأس المال بحكمة لضمان أفضل النتائج! 🚀"""

METIS_LOWER = """🔵 metis / USDT 🤖📈

Exchange: okx

Buy Zone: 3.09 – 2.94

Targets:
T1: 3.17
T2: 3.24
T3: 3.40
T4: 3.65
T5:4

Stop Loss: 2.86"""

FUTURES_BOT = """AT / USDT 🤖📈

EXCHANGE : OKX
FUTURES LONG 10X

Buy Zone: 0.1360

Targets:
T1: 0.1470
T2: 0.1539

Stop Loss: 0.1290"""

FUTURES_MANUAL = """Xlm long 10x
Entry 0.2300
Tp1 0.2400
Tp2 0.2450
🛑 stop 0.2210"""

NO_STOP = """🔵Op/usdt 🤖📈
Exchange: okx
Future or spot
Buy Zone: 0.0890 - 0.0850
Targets:
T1: 0.0912
T2: 0.0934"""


def test_bot_post_parses_as_binance_spot_with_equal_target_slices():
    sig = parse_any(VIRTUAL)
    assert sig is not None
    assert sig.symbol == "VIRTUALUSDT" and sig.exchange == "binance"
    assert (sig.entry_low, sig.entry_high) == (0.75, 0.7786)  # zone written high – low
    assert sig.stop_price == pytest.approx(0.6827)
    assert [t.price for t in sig.targets] == [0.7919, 0.8175, 0.8408, 0.9343, 1.0121]
    assert sum(t.sell_fraction for t in sig.targets) == pytest.approx(1.0)
    assert all(t.sell_fraction == pytest.approx(0.2) for t in sig.targets)
    assert validate_signal(sig) == []


def test_lowercase_symbol_and_integer_target():
    sig = parse_signal_v3(METIS_LOWER)
    assert sig.symbol == "METISUSDT"
    assert sig.targets[-1].price == 4.0


@pytest.mark.parametrize("text", [FUTURES_BOT, FUTURES_MANUAL])
def test_futures_posts_are_never_signals(text):
    assert is_futures(text)
    assert parse_any(text) is None
    assert not looks_like_any_signal(text)


@pytest.mark.parametrize("text", [
    "#DOGE/USDT Entered entry zone ✅",
    "#VIRTUAL/USDT Take-Profit target 2 ✅\nProfit: 3.9888% 📈\nPeriod: 1 day 1 hr ⏰",
    "#XLM/USDT Stop Target Hit ⛔\nLoss: 39.1304% 📉",
    NO_STOP,  # no stop: never traded
])
def test_progress_posts_and_stopless_posts_are_ignored(text):
    assert parse_any(text) is None


def test_listener_retries_after_a_failed_start(monkeypatch):
    calls = {"n": 0}

    class FakeClient:
        async def is_user_authorized(self):
            return True

    async def flaky_client():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return FakeClient()

    async def listen(client):
        tl._state["status"] = "connected"

    monkeypatch.setattr(tl, "is_configured", lambda: True)
    monkeypatch.setattr(tl, "_ensure_client", flaky_client)
    monkeypatch.setattr(tl, "_begin_listening", listen)
    monkeypatch.setattr(tl, "RETRY_BASE_DELAY_S", 0.0)
    monkeypatch.setattr(tl, "_retry_task", None)
    monkeypatch.setitem(tl._state, "retry_attempt", 0)

    async def scenario():
        await tl.start()
        assert tl._state["status"] == "error"
        await tl._retry_task
        return tl._state["status"]

    assert asyncio.run(scenario()) == "connected"
    assert calls["n"] == 2


def _run_one_poll(monkeypatch):
    """Run _poll_loop for exactly one iteration."""
    sleeps = {"n": 0}

    async def fake_sleep(_s):
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(tl.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(tl._poll_loop())
    except asyncio.CancelledError:
        pass


def test_poll_picks_up_posts_the_push_updates_missed(monkeypatch):
    class Client:
        def is_connected(self):
            return True

    seen = []

    async def catch_up(client, channel, entity):
        seen.append(channel)
        return 2

    monkeypatch.setattr(tl, "_client", Client())
    monkeypatch.setitem(tl._state, "status", "connected")
    monkeypatch.setattr(tl, "_WATCHED", [("signal252", object()), ("Shaban vip", object())])
    monkeypatch.setattr(tl, "_catch_up", catch_up)
    _run_one_poll(monkeypatch)
    assert seen == ["signal252", "Shaban vip"]


def test_poll_restarts_a_listener_that_is_not_connected(monkeypatch):
    restarted = []

    async def fake_start():
        restarted.append(True)

    monkeypatch.setattr(tl, "_client", None)
    monkeypatch.setitem(tl._state, "status", "error")
    monkeypatch.setattr(tl, "start", fake_start)
    _run_one_poll(monkeypatch)
    assert restarted == [True]
