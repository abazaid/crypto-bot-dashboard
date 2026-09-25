"""Parser tests on transcriptions of real channel posts (no network)."""
import pytest

from app.services.telegram_signals import parse_signal, validate_signal

pytestmark = pytest.mark.unit

ALICE = """🚀 SHAABAN ELITE SIGNAL

💎 #ALICE | Binance

📍 الدخول: 0.1480 – 0.1556

🎯 الأهداف:

1️⃣ 0.171 | +9.9% | بيع 20%
2️⃣ 0.187 | +20.2% | بيع 25%
3️⃣ 0.210 | +35.0% | بيع 25%
4️⃣ 0.233 | +49.7% | بيع 30%

🛑 الستوب: إغلاق 4 ساعات أسفل 0.137

⚠️ تقسيم الدخول وإدارة رأس المال."""

HOLO_REVERSED_ZONE = """🚀 SHAABAN ELITE SIGNAL
💎 #HOLO | Binance
📍 الدخول: 0.0679 – 0.06200
🎯 الأهداف:
1️⃣ 0.0700 | +3.1% | بيع 20%
2️⃣ 0.07551 | +11.2% | بيع 25%
3️⃣ 0.08420 | +24.0% | بيع 25%
4️⃣ 0.09776 | +44.0% | بيع 30%
🛑 الستوب: إغلاق 4 ساعات أسفل 0.0600
⚠️ تقسيم الدخول وإدارة رأس المال."""

ZERO_G_EDITED = """🚀 SHAABAN ELITE SIGNAL
💎 #0G | Binance
📍 الدخول: 0.2200 – 0.2564
🎯 الأهداف:
1️⃣ 0.2652 | +3.4% | بيع 20% ✅
2️⃣ 0.2754 | +7.4% | بيع 25% ✅
3️⃣ 0.2933 | +14.4% | بيع 25%
4️⃣ 0.3315 | +29.3% | بيع 30%
🛑 الستوب: إغلاق 4 ساعات أسفل 0.2000
⚠️ تقسيم الدخول وإدارة رأس المال.
🏆 الصفقة اكتملت بنجاح."""

QNT = """💎 #QNT | Binance
📍 الدخول: 63.50 – 61.00
🎯 الأهداف:
1️⃣ 69.85 | +10.0% | بيع 20%
2️⃣ 76.20 | +20.0% | بيع 25%
3️⃣ 85.73 | +35.0% | بيع 25%
4️⃣ 95.25 | +50.0% | بيع 30%
🛑 الستوب: إغلاق 4 ساعات أسفل 59.00"""


def test_parse_alice():
    s = parse_signal(ALICE)
    assert s is not None
    assert s.symbol == "ALICEUSDT" and s.is_binance
    assert (s.entry_low, s.entry_high) == (0.1480, 0.1556)
    assert s.stop_price == 0.137 and s.stop_on_4h_close
    assert [t.price for t in s.targets] == [0.171, 0.187, 0.210, 0.233]
    assert [t.pct for t in s.targets] == [9.9, 20.2, 35.0, 49.7]
    assert [round(t.sell_fraction, 2) for t in s.targets] == [0.2, 0.25, 0.25, 0.3]
    assert validate_signal(s) == []


def test_reversed_entry_zone_is_normalised():
    s = parse_signal(HOLO_REVERSED_ZONE)
    assert s is not None
    assert (s.entry_low, s.entry_high) == (0.062, 0.0679)
    assert s.stop_price == 0.06
    assert validate_signal(s) == []


def test_digit_symbol_and_edited_marks():
    s = parse_signal(ZERO_G_EDITED)
    assert s is not None
    assert s.symbol == "0GUSDT"
    assert len(s.targets) == 4 and s.targets[0].price == 0.2652
    assert s.stop_price == 0.2
    assert validate_signal(s) == []


def test_large_prices():
    s = parse_signal(QNT)
    assert s is not None
    assert (s.entry_low, s.entry_high) == (61.0, 63.5)
    assert s.targets[-1].price == 95.25 and s.stop_price == 59.0
    assert validate_signal(s) == []


def test_non_signal_and_other_exchange():
    assert parse_signal("صباح الخير للجميع، السوق اليوم هادئ") is None
    s = parse_signal(ALICE.replace("| Binance", "| Bybit"))
    assert s is not None and not s.is_binance
    assert any("not Binance" in p for p in validate_signal(s))


def test_validation_catches_bad_levels():
    bad = ALICE.replace("أسفل 0.137", "أسفل 0.160")  # stop inside/above zone
    s = parse_signal(bad)
    assert s is not None
    assert any("stop is not below" in p for p in validate_signal(s))


def test_thousands_separator_and_note_on_entry_line():
    from app.services.telegram_signals import parse_signal, validate_signal

    text = """💎 #BTC | Binance
📍 الدخول (منطقة 1): 67,100 – 67,900.50
🎯 الأهداف:
1️⃣ 69,850 | +3.4% | بيع 20%
2️⃣ 71,000 | +5.0% | بيع 30%
3️⃣ 74,500.25 | +10.0% | بيع 50%
🛑 الستوب: إغلاق 4 ساعات أسفل 65,000"""
    s = parse_signal(text)
    assert s is not None
    assert (s.entry_low, s.entry_high) == (67100.0, 67900.5)
    assert [t.price for t in s.targets] == [69850.0, 71000.0, 74500.25]
    assert s.stop_price == 65000.0
    assert validate_signal(s) == []
