"""Tolerant fallback parser (v5): improvised layouts without colons, "#" or a stop."""
from __future__ import annotations

import pytest

from app.services.telegram_signals import looks_like_any_signal, parse_any, validate_signal

pytestmark = pytest.mark.unit

API3 = "Api3usdt spot\nOkx\nEntry 0.2930\nTp1 0.3430\nTp2 0.4267\nTp3 0.5061"  # Shaban vip, 2026-10-04


def test_api3_post_without_colons_or_stop_is_a_market_entry_with_bot_stop():
    sig = parse_any(API3)
    assert sig.symbol == "API3USDT"
    assert sig.is_binance
    assert sig.entry_low == sig.entry_high == pytest.approx(0.2930)
    assert sig.entry_kind == "market"
    assert [t.price for t in sig.targets] == [0.3430, 0.4267, 0.5061]
    assert sum(t.sell_fraction for t in sig.targets) == pytest.approx(1.0)
    assert sig.stop_price == pytest.approx(0.2930 * 0.9)
    assert sig.warnings
    assert validate_signal(sig) == []


def test_zone_with_bare_target_lines_and_stop():
    sig = parse_any("ARB/USDT\nBuy zone 0.50 - 0.47\nTargets\n0.55\n0.60\n0.70\nStop 0.43")
    assert (sig.entry_low, sig.entry_high) == (0.47, 0.50)
    assert sig.entry_kind == "zone"
    assert [t.price for t in sig.targets] == [0.55, 0.60, 0.70]
    assert sig.stop_price == 0.43
    assert not sig.warnings


def test_numbered_targets_and_dash_symbol():
    sig = parse_any("SOL-USDT\nentry: 140\ntargets:\n1) 150\n2) 160\nsl 128")
    assert sig.symbol == "SOLUSDT"
    assert [t.price for t in sig.targets] == [150, 160]
    assert sig.stop_price == 128


@pytest.mark.parametrize("text", [
    "Tp1 0.343 hit ✅ api3usdt",
    "Api3usdt\n1️⃣ 0.343✅\n2️⃣ 0.42\nEntry 0.29",
    "API3 FUTURES LONG 10X Entry 0.29 TP1 0.34",
    "Api3usdt closed\nEntry 0.29\nTp1 0.34",
    "Good morning, BTC above 100 USDT",
])
def test_updates_futures_and_chatter_are_not_signals(text):
    assert parse_any(text) is None
    assert not looks_like_any_signal(text)


def test_unparseable_post_is_still_recorded_not_dropped():
    # names a coin, an entry and targets, but no usable target price: ingest records it as invalid
    text = "Api3usdt spot\nEntry 0.2930\nTargets soon"
    assert looks_like_any_signal(text)
    assert parse_any(text) is None
