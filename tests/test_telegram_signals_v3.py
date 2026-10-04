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
])
def test_progress_posts_are_ignored(text):
    assert parse_any(text) is None


def test_stopless_post_gets_the_bot_default_stop():
    sig = parse_any(NO_STOP)
    assert sig.symbol == "OPUSDT"
    assert sig.stop_price == pytest.approx(0.0850 * 0.9)
    assert [t.price for t in sig.targets] == [0.0912, 0.0934]
    assert validate_signal(sig) == []


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


ZEN_NEW = "💎 #ZEN | OKX spot \n\n📍 Entry: 6.700 6.400\n\n🎯 Targets:\n\n1️⃣ 7.275\n2️⃣ 8.400\n3️⃣ 9.600\n\n🛑5.760 CLOSE 1D"
JOE_NEW = "💎 #JOE | okx\n\n📍 Entry: 0.0365 – 0.0392\n\n🎯 Targets:\n\n1️⃣ 0.0431 \n2️⃣ 0.0470 \n3️⃣ 0.0529 \n4️⃣ 0.0588 \n\n🛑0.0300"


def test_new_short_layout_parses():
    zen = parse_any(ZEN_NEW)
    assert zen.symbol == "ZENUSDT" and zen.exchange == "binance"
    assert (zen.entry_low, zen.entry_high, zen.stop_price) == (6.4, 6.7, 5.76)
    assert [t.price for t in zen.targets] == [7.275, 8.4, 9.6]
    assert validate_signal(zen) == []
    joe = parse_any(JOE_NEW)
    assert (joe.symbol, joe.entry_low, joe.entry_high, joe.stop_price) == ("JOEUSDT", 0.0365, 0.0392, 0.03)
    assert [t.price for t in joe.targets] == [0.0431, 0.047, 0.0529, 0.0588]


@pytest.mark.parametrize("text", [
    "#JOE/USDT Entry 1 ✅\nAverage Entry Price: 0.03920 💵",
    "Xlm long 10x\nEntry 0.2300\nTp1 0.2400\nTp2 0.2450\nTp3 0.2500\n🛑 stop 0.2210",
])
def test_new_layout_does_not_catch_progress_or_futures_posts(text):
    assert parse_any(text) is None


RENDER_AUG = """🚀 SHAABAN ELITE SIGNAL

💎 #RENDER | Binance

📍 منطقة الدخول: 1.330 – 1.372

🎯 الأهداف:
1️⃣ 1.850
2️⃣ 2.650
3️⃣ 3.100
4️⃣ 3.950

🛑 وقف الخسارة: إغلاق شمعة يومية أسفل 1.270"""

ICP_AUG_SPLIT_LINES = """💎 #ICP | Binance

📍 منطقة الدخول:
2.022 – 2.076

🎯 الأهداف:
1️⃣ 2.20✅
2️⃣ 2.35✅

🛑 وقف الخسارة:
إغلاق شمعة 4 ساعات أسفل 1.87"""

ZERO_G_V4 = """🚀 SHAABAN ELITE SIGNAL

💎 #0G | 15 m
⚡ Strong Setup | 9.8/10

🏦 Available on: Binance | KuCoin | MEXC
📊 Signal based on: Binance

💰 Entry: $0.181
🛑 SL: $0.163

🎯 Targets
• TP1: $0.19 (+4.97%)
• TP2: $0.2 (+10.50%)
• TP3: $0.24 (+32.60%)
• TP4: $0.27 (+49.17%)

⏳ Live Opportunity"""

ASTR_ARROWS = """🚀 SHAABAN SIGNAL

💎 #ASTR | ⏰ 15 m

💰 Entry: $0.008170
🛑 SL: $0.007467

🎯 Targets:
⬜ TP1 → $0.008578 (+5.0%)
⬜ TP2 → $0.008986 (+10.0%)"""

T_SINGLE_LETTER = ZERO_G_V4.replace("#0G", "#T")
KUCOIN_ONLY = ZERO_G_V4.replace("Available on: Binance | KuCoin | MEXC", "Available on: KuCoin | MEXC")


def test_arabic_zone_layout_takes_the_level_after_asfal_not_the_hours():
    r = parse_any(RENDER_AUG)
    assert (r.symbol, r.entry_low, r.entry_high, r.stop_price) == ("RENDERUSDT", 1.33, 1.372, 1.27)
    assert [t.price for t in r.targets] == [1.85, 2.65, 3.1, 3.95]
    icp = parse_any(ICP_AUG_SPLIT_LINES)  # values on the line after the label, ✅ marks after targets
    assert (icp.entry_low, icp.entry_high, icp.stop_price) == (2.022, 2.076, 1.87)
    assert [t.price for t in icp.targets] == [2.2, 2.35]


@pytest.mark.parametrize("text,symbol,entry,stop,targets", [
    (ZERO_G_V4, "0GUSDT", 0.181, 0.163, [0.19, 0.2, 0.24, 0.27]),
    (ASTR_ARROWS, "ASTRUSDT", 0.00817, 0.007467, [0.008578, 0.008986]),
    (T_SINGLE_LETTER, "TUSDT", 0.181, 0.163, [0.19, 0.2, 0.24, 0.27]),
])
def test_single_price_entry_layouts_are_market_entries(text, symbol, entry, stop, targets):
    s = parse_any(text)
    assert (s.symbol, s.entry_low, s.entry_high, s.stop_price) == (symbol, entry, entry, stop)
    assert s.entry_kind == "market"
    assert [t.price for t in s.targets] == targets
    assert validate_signal(s) == []


def test_kucoin_only_signal_is_not_binance():
    assert any("not Binance" in p for p in validate_signal(parse_any(KUCOIN_ONLY)))


@pytest.mark.parametrize("text", [
    ZERO_G_V4.replace("⏳ Live Opportunity", "🏁 Closed | 🏆 Profit: +10.01%"),  # edited after the fact
    "🟡 تم الإغلاق\n🔹 العملة: #CHR\n💰 السعر: $0.0201\n🛑 الوقف: $0.0185\n• TP1: $0.0211 (+4.98%)",
    "🎯 تحقق الهدف 1 (+4.97%) - #APT\n• TP1: $0.992 (+4.97%)",
])
def test_closed_and_progress_posts_are_not_signals(text):
    assert parse_any(text) is None
