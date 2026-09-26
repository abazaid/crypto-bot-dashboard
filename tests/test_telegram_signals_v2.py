"""Real posts from @Ox3rwah_eth (transcribed) — signals vs. everything else."""
import pytest

from app.services.telegram_signals import parse_any, validate_signal
from app.services.telegram_signals_v2 import looks_like_signal_v2, parse_signal_v2

pytestmark = pytest.mark.unit

DODO = """DODO
🟢 دخول فوري
0.0184 $
🟡 دخول ثاني (-5.0%)
0.0175 $
🎯هدف 1: 0.0192  (+4.0%)
🎯هدف 2: 0.0195  (+6.0%)
🎯هدف 3: 0.0199  (+7.9%)
🎯هدف 4: 0.0203  (+10.1%)
🎯هدف 5: 0.0211  (+14.4%)
⛔️وقف: 0.0168  (-4.0% تحت آخر دخول)"""

AXL_SINGLE_ENTRY = """AXL
🟢 دخول فوري
0.051 $
🎯هدف 1: 0.053  (+4.0%)
🎯هدف 2: 0.054  (+5.9%)
🎯هدف 3: 0.0549  (+7.7%)
🎯هدف 4: 0.056  (+9.9%)
🎯هدف 5: 0.0572  (+12.2%)
⛔️وقف: 0.0489  (-4.0%)"""

PUNDIX_RISK = """PUNDIX
⚠️ خطورة متوسطة
🟢 دخول فوري
0.0971 $
🟡 دخول ثاني (-5.0%)
0.0922 $
🎯هدف 1: 0.1  (+4.0%)
🎯هدف 2: 0.103  (+6.1%)
🎯هدف 3: 0.104  (+8.1%)
🎯هدف 4: 0.107  (+10.3%)
🎯هدف 5: 0.11  (+14.3%)
⛔️وقف: 0.0894  (-3.0% تحت آخر دخول)"""

BONK_TINY = """BONK
🟢 دخول فوري
0.00000336 $
🟡 دخول ثاني (-7.0%)
0.00000312 $
🎯هدف 1: 0.00000349  (+4.0%)
🎯هدف 2: 0.00000361  (+7.7%)
🎯هدف 3: 0.00000369  (+10.1%)
🎯هدف 4: 0.00000384  (+14.5%)
🎯هدف 5: 0.0000041  (+22.2%)
⛔️وقف: 0.00000296  (-5.0% تحت آخر دخول)"""

PUNDIX_2ND_ENTRY_EDITED_STOP = """PUNDIX 🎯
⚠️ خطورة متوسطة
🎯هدف 1: 0.0984  (+4.0%)✅
🎯هدف 2: 0.1  (+6.1%)✅
🎯هدف 3: 0.102  (+8.1%)✅
🎯هدف 4: 0.104  (+10.3%)✅
🎯هدف 5: 0.108  (+14.3%)✅
⛔️الوقف: 0.0894  (-3.0% تحت آخر دخول)"""

PROGRESS = """AXL 🎯
🎯هدف 1: 0.053  (+4.0%)✅
🎯هدف 2: 0.054  (+5.9%)✅"""

AVERAGED = """✅ تم تفعيل الدخول الثاني — حُسب المتوسط بافتراض تساوي الكميتين

PUNDIX
⚠️ خطورة متوسطة
🟢 متوسط الدخول
0.0946 $
🎯هدف 1: 0.0984  (+4.0%)
🎯هدف 2: 0.1  (+6.1%)
⛔️وقف: 0.0894  (-3.0% تحت آخر دخول)"""

LEVEL = """SOLV 🔥
➕2️⃣0️⃣🛍 🚀
سعر المستوى: 0.00353"""

CANCELLED = """⚠️ أُلغي الدخول الثاني المعلّق تلقائيًا بعد تحقق الهدف الأول.

BONK 🎯
🎯هدف 1: 0.00000349  (+4.0%)✅"""

PRE_ANNOUNCE = """2z
دخول
وقف واهداف لاحقاً"""


def test_full_signal_with_second_entry():
    s = parse_signal_v2(DODO)
    assert s is not None
    assert s.symbol == "DODOUSDT" and s.entry_kind == "market"
    assert s.entry_high == 0.0184 and s.leg2_price == 0.0175 and s.entry_low == 0.0175
    assert s.stop_price == 0.0168
    assert [t.price for t in s.targets] == [0.0192, 0.0195, 0.0199, 0.0203, 0.0211]
    assert [t.pct for t in s.targets] == [4.0, 6.0, 7.9, 10.1, 14.4]
    assert sum(t.sell_fraction for t in s.targets) == pytest.approx(1.0)
    assert s.targets[0].sell_fraction == pytest.approx(0.2)
    assert validate_signal(s) == []


def test_single_entry_signal():
    s = parse_signal_v2(AXL_SINGLE_ENTRY)
    assert s is not None
    assert s.leg2_price is None and s.entry_low == s.entry_high == 0.051
    assert s.stop_price == 0.0489 and len(s.targets) == 5
    assert validate_signal(s) == []


def test_risk_note_and_tiny_prices():
    s = parse_signal_v2(PUNDIX_RISK)
    assert s is not None and s.risk_note and "متوسطة" in s.risk_note
    assert s.entry_high == 0.0971 and s.leg2_price == 0.0922 and s.stop_price == 0.0894
    b = parse_signal_v2(BONK_TINY)
    assert b is not None and b.entry_high == 3.36e-06 and b.stop_price == 2.96e-06 and b.targets[-1].price == 4.1e-06
    assert validate_signal(b) == []


def test_non_signals_are_ignored():
    for text in (PROGRESS, AVERAGED, LEVEL, CANCELLED, PRE_ANNOUNCE, PUNDIX_2ND_ENTRY_EDITED_STOP, "https://x.com/OrwahKalthoum/status/1", "ضرب الوقف"):
        assert not looks_like_signal_v2(text), text[:30]
        assert parse_signal_v2(text) is None, text[:30]


def test_dispatcher_handles_both_formats():
    from test_telegram_signals import ALICE

    a = parse_any(ALICE)
    b = parse_any(DODO)
    assert a is not None and a.entry_kind == "zone" and a.symbol == "ALICEUSDT"
    assert b is not None and b.entry_kind == "market" and b.symbol == "DODOUSDT"
    assert parse_any(PROGRESS) is None
