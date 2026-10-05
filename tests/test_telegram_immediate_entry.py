"""@Ox3rwah_eth "دخول فوري" posts: buy now even though the posted price is written with digits dropped."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import telegram_signal_service as svc
from app.services.telegram_signals_v2 import parse_signal_v2

pytestmark = pytest.mark.unit

NIL = """NIL
🟢 دخول فوري
0.1 $
🟡 دخول ثاني (-5.0%)
0.0956 $
🎯هدف 1: 0.104  (+4.0%)
🎯هدف 2: 0.106  (+6.0%)
🎯هدف 3: 0.108  (+8.2%)
🎯هدف 4: 0.112  (+11.2%)
🎯هدف 5: 0.115  (+14.9%)
⛔️وقف: 0.0918  (-4.0% تحت آخر دخول)"""


def _row(text: str, entry_kind: str = "market") -> SimpleNamespace:
    sig = parse_signal_v2(text)
    return SimpleNamespace(
        status="pending_entry", status_note=None, expires_at=datetime.utcnow() + timedelta(hours=1),
        symbol=sig.symbol, entry_low=sig.entry_low, entry_high=sig.entry_high, stop_price=sig.stop_price,
        entry_kind=entry_kind, raw_text=text, posted_at=None, created_at=None,
        targets_json=json.dumps([{"price": t.price} for t in sig.targets]),
    )


def _attempt(row: SimpleNamespace, price: float) -> str:
    pool = SimpleNamespace(status="paused")  # passing the price gate lands on the "pool is paused" stop
    with patch.object(svc, "_high_since", return_value=None):
        svc._try_enter(MagicMock(), pool, row, price=price)
    return row.status_note or ""


def test_parser_flags_immediate():
    assert getattr(parse_signal_v2(NIL), "immediate", False) is True


def test_immediate_entry_buys_above_rounded_price():
    # the real case: posted 0.1, live 0.10206 (+2.06%) used to wait forever
    assert "pool is paused" in _attempt(_row(NIL), 0.10206)


def test_immediate_entry_still_capped():
    far_t1 = NIL.replace("هدف 1: 0.104", "هدف 1: 0.110")
    assert "never chase" in _attempt(_row(far_t1), 0.1045)  # > 4% premium, T1 still far


def test_immediate_entry_needs_room_to_target_1():
    assert "too close to target 1" in _attempt(_row(NIL), 0.1035)


def test_zone_entry_unchanged():
    assert "above the entry zone" in _attempt(_row(NIL, entry_kind="zone"), 0.10206)
