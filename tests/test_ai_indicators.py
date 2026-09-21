import math

import pytest

from app.services import ai_indicators as ind

pytestmark = pytest.mark.unit


def _series(n: int, start: float = 100.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(n)]


def test_sma_and_ema_basic():
    vals = _series(10)
    assert ind.sma(vals, 5) == pytest.approx(sum(vals[-5:]) / 5)
    e = ind.ema(vals, 5)
    assert e is not None and 105.0 < e < 109.0
    assert ind.ema(vals, 50) is None


def test_rsi_bounds_and_direction():
    up = _series(40, step=1.0)
    down = _series(40, step=-1.0)
    assert ind.rsi(up, 14) == pytest.approx(100.0)
    assert ind.rsi(down, 14) == pytest.approx(0.0)
    flat_then_up = [100.0] * 20 + _series(20, 100.0, 0.5)
    r = ind.rsi(flat_then_up, 14)
    assert r is not None and 50.0 < r <= 100.0


def test_atr_constant_range():
    n = 40
    highs = [101.0] * n
    lows = [99.0] * n
    closes = [100.0] * n
    assert ind.atr(highs, lows, closes, 14) == pytest.approx(2.0)


def test_donchian_excludes_current_bar():
    highs = [10.0] * 20 + [50.0]
    lows = [5.0] * 20 + [1.0]
    upper, lower = ind.donchian(highs, lows, 20, exclude_last=True)
    assert upper == 10.0 and lower == 5.0
    upper2, _ = ind.donchian(highs, lows, 20, exclude_last=False)
    assert upper2 == 50.0


def test_bollinger_symmetry():
    vals = [100.0 + (i % 2) for i in range(30)]
    upper, mid, lower = ind.bollinger(vals, 20, 2.0)
    assert mid == pytest.approx(100.5)
    assert upper - mid == pytest.approx(mid - lower)


def test_adx_strong_trend_is_high():
    n = 80
    closes = _series(n, 100.0, 1.0)
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    a = ind.adx(highs, lows, closes, 14)
    assert a is not None and a > 50.0


def test_supertrend_direction_in_uptrend():
    n = 60
    closes = _series(n, 100.0, 1.0)
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    direction, line = ind.supertrend(highs, lows, closes, 10, 3.0)
    assert direction == 1
    assert line < closes[-1]


def test_macd_hist_series_length_and_sign():
    closes = _series(120, 100.0, 0.5)
    hist = ind.macd_hist_series(closes)
    assert len(hist) > 50
    assert all(not math.isnan(h) for h in hist)


def test_pct_return_and_volume_ratio():
    vals = [100.0] * 10 + [110.0]
    assert ind.pct_return(vals, 1) == pytest.approx(10.0)
    vols = [1.0] * 20 + [3.0]
    assert ind.volume_ratio(vols, 20) == pytest.approx(3.0)


def test_candle_patterns():
    assert ind.is_hammer(o=10.0, h=10.2, lo=8.0, c=10.1)
    assert not ind.is_hammer(o=10.0, h=12.0, lo=9.9, c=10.1)
    assert ind.is_bullish_engulfing(prev_o=10.0, prev_c=9.5, o=9.4, c=10.2)
    assert not ind.is_bullish_engulfing(prev_o=9.5, prev_c=10.0, o=9.4, c=10.2)
