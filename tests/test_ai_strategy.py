"""Strategy engine tests on synthetic klines. No network."""
from __future__ import annotations

import random

import pytest

from app.services import ai_strategy as strat

pytestmark = pytest.mark.unit


def _klines(closes: list[float], vol: float = 1000.0, spread: float = 0.01, vols: list[float] | None = None) -> list[list]:
    rows = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i > 0 else c
        h = max(o, c) * (1 + spread)
        lo = min(o, c) * (1 - spread)
        v = vols[i] if vols else vol
        rows.append([i, o, h, lo, c, v])
    return rows


def _uptrend_breakout_set():
    """230 4h bars: slow uptrend, flat consolidation, then a volume breakout on the last closed bar."""
    random.seed(7)
    closes = []
    p = 100.0
    for i in range(200):
        p *= 1 + random.uniform(-0.004, 0.009)
        closes.append(p)
    top = max(closes[-25:])
    for _ in range(28):
        closes.append(top * random.uniform(0.96, 0.99))
    closes.append(top * 1.03)  # last CLOSED bar breaks the 20-bar high
    closes.append(top * 1.031)  # forming bar
    vols = [1000.0] * (len(closes) - 2) + [2500.0, 1200.0]
    kl4h = _klines(closes, vols=vols)
    # 1h and 1d series consistent with an uptrend
    kl1h = _klines([closes[-1] * (1 + 0.0005 * i) for i in range(160)])
    daily = [closes[max(0, len(closes) - 1 - (69 - i) * 6)] for i in range(70)]
    kl1d = _klines(daily)
    return kl4h, kl1h, kl1d


def test_breakout_signal_detected_in_uptrend():
    kl4h, kl1h, kl1d = _uptrend_breakout_set()
    sig = strat.evaluate_symbol("TESTUSDT", "bullish", kl4h=kl4h, kl1h=kl1h, kl1d=kl1d)
    assert sig is not None
    assert sig.strategy in {"breakout", "squeeze"}
    assert sig.stop_price < sig.price
    assert sig.tp1_price > sig.price
    assert 0.8 <= sig.risk_pct <= 12.0
    assert sig.score >= 55


def test_no_signal_in_strong_bearish_regime():
    kl4h, kl1h, kl1d = _uptrend_breakout_set()
    assert strat.evaluate_symbol("TESTUSDT", "strong_bearish", kl4h=kl4h, kl1h=kl1h, kl1d=kl1d) is None


def test_no_signal_in_downtrend():
    random.seed(3)
    closes = []
    p = 100.0
    for _ in range(230):
        p *= 1 + random.uniform(-0.01, 0.004)
        closes.append(p)
    kl4h = _klines(closes)
    kl1h = _klines(closes[-160:])
    kl1d = _klines(closes[-70:])
    assert strat.evaluate_symbol("TESTUSDT", "bullish", kl4h=kl4h, kl1h=kl1h, kl1d=kl1d) is None


def test_tradeable_symbol_filter():
    assert strat.is_tradeable_symbol("ETHUSDT")
    assert not strat.is_tradeable_symbol("ETHUPUSDT")
    assert not strat.is_tradeable_symbol("USDCUSDT")
    assert not strat.is_tradeable_symbol("BTCUSDT")  # loop-excluded by settings default
    assert not strat.is_tradeable_symbol("ETHBTC")


def test_regime_min_score_adjust():
    assert strat.regime_min_score_adjust("bullish") == 0.0
    assert strat.regime_min_score_adjust("bearish") > strat.regime_min_score_adjust("neutral") > 0.0


def test_zero_volume_hours_are_skipped():
    kl4h, kl1h, kl1d = _uptrend_breakout_set()
    kl1h[-10][5] = 0.0  # one closed hour with no trades (market closed)
    assert strat.evaluate_symbol("TESTUSDT", "bullish", kl4h=kl4h, kl1h=kl1h, kl1d=kl1d) is None


def test_tokenized_stocks_excluded():
    assert not strat.is_tradeable_symbol("SNDKBUSDT")
    assert not strat.is_tradeable_symbol("CRCLBUSDT")
