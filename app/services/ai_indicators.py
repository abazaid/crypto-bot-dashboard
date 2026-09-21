"""
Pure technical-indicator functions for the AI Trader engine.

Everything here is side-effect free and works on plain Python lists so it can
be unit-tested without touching any exchange API.

Kline row layout (Binance): [open_time, open, high, low, close, volume, ...]
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Ohlcv:
    opens: list[float]
    highs: list[float]
    lows: list[float]
    closes: list[float]
    volumes: list[float]

    def __len__(self) -> int:
        return len(self.closes)


def parse_klines(klines: Sequence[Sequence]) -> Ohlcv:
    """Convert raw Binance kline rows into column lists."""
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    volumes: list[float] = []
    for k in klines:
        opens.append(float(k[1]))
        highs.append(float(k[2]))
        lows.append(float(k[3]))
        closes.append(float(k[4]))
        volumes.append(float(k[5]))
    return Ohlcv(opens, highs, lows, closes, volumes)


# ── Moving averages ────────────────────────────────────────────────────────────

def sma(values: Sequence[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: Sequence[float], period: int) -> list[float]:
    """Full EMA series (same length as input; first period-1 entries are NaN)."""
    if period <= 0 or len(values) < period:
        return []
    k = 2.0 / (period + 1.0)
    out: list[float] = []
    seed = sum(values[:period]) / period
    for i, v in enumerate(values):
        if i < period - 1:
            out.append(float("nan"))
        elif i == period - 1:
            out.append(seed)
        else:
            out.append(v * k + out[-1] * (1.0 - k))
    return out


def ema(values: Sequence[float], period: int) -> float | None:
    s = ema_series(values, period)
    return s[-1] if s else None


# ── Oscillators ────────────────────────────────────────────────────────────────

def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """Wilder RSI."""
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        g = d if d > 0 else 0.0
        lo = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + lo) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd_hist_series(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> list[float]:
    """MACD histogram series (aligned to the tail of the input)."""
    if len(values) < slow + signal + 5:
        return []
    ef = ema_series(values, fast)
    es = ema_series(values, slow)
    line = [f - s for f, s in zip(ef, es) if f == f and s == s]
    sig = ema_series(line, signal)
    if not sig:
        return []
    return [ln - sg for ln, sg in zip(line, sig) if sg == sg]


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float, float] | None:
    """Return (macd_line, signal_line, histogram) for the last bar."""
    if len(values) < slow + signal + 5:
        return None
    ef = ema_series(values, fast)
    es = ema_series(values, slow)
    line = [f - s for f, s in zip(ef, es) if f == f and s == s]
    sig = ema_series(line, signal)
    if not sig:
        return None
    return line[-1], sig[-1], line[-1] - sig[-1]


# ── Volatility ─────────────────────────────────────────────────────────────────

def true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    out: list[float] = []
    for i in range(len(closes)):
        if i == 0:
            out.append(highs[i] - lows[i])
            continue
        prev_close = closes[i - 1]
        out.append(max(highs[i] - lows[i], abs(highs[i] - prev_close), abs(lows[i] - prev_close)))
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> float | None:
    """Wilder ATR."""
    if len(closes) < period + 1:
        return None
    tr = true_ranges(highs, lows, closes)
    a = sum(tr[1 : period + 1]) / period
    for i in range(period + 1, len(tr)):
        a = (a * (period - 1) + tr[i]) / period
    return a


def bollinger(values: Sequence[float], period: int = 20, mult: float = 2.0) -> tuple[float, float, float] | None:
    """Return (upper, middle, lower)."""
    if len(values) < period:
        return None
    window = values[-period:]
    mid = sum(window) / period
    var = sum((v - mid) ** 2 for v in window) / period
    sd = var ** 0.5
    return mid + mult * sd, mid, mid - mult * sd


def bollinger_bandwidth_series(values: Sequence[float], period: int = 20, mult: float = 2.0) -> list[float]:
    out: list[float] = []
    for i in range(period, len(values) + 1):
        window = values[i - period : i]
        mid = sum(window) / period
        if mid <= 0:
            out.append(0.0)
            continue
        var = sum((v - mid) ** 2 for v in window) / period
        sd = var ** 0.5
        out.append((2.0 * mult * sd) / mid)
    return out


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> float | None:
    """Wilder ADX (trend strength, 0-100)."""
    n = len(closes)
    if n < period * 2 + 1:
        return None
    tr = true_ranges(highs, lows, closes)
    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)

    atr_s = sum(tr[1 : period + 1])
    pdm_s = sum(plus_dm[1 : period + 1])
    mdm_s = sum(minus_dm[1 : period + 1])
    dx_values: list[float] = []
    for i in range(period + 1, n):
        atr_s = atr_s - atr_s / period + tr[i]
        pdm_s = pdm_s - pdm_s / period + plus_dm[i]
        mdm_s = mdm_s - mdm_s / period + minus_dm[i]
        if atr_s <= 0:
            dx_values.append(0.0)
            continue
        pdi = 100.0 * pdm_s / atr_s
        mdi = 100.0 * mdm_s / atr_s
        denom = pdi + mdi
        dx_values.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0)
    if len(dx_values) < period:
        return None
    a = sum(dx_values[:period]) / period
    for v in dx_values[period:]:
        a = (a * (period - 1) + v) / period
    return a


# ── Channels & trend tools ─────────────────────────────────────────────────────

def donchian(highs: Sequence[float], lows: Sequence[float], period: int = 20, exclude_last: bool = True) -> tuple[float, float] | None:
    """(upper, lower) of the prior period bars. With exclude_last the current bar is not part of the channel."""
    end = len(highs) - 1 if exclude_last else len(highs)
    if end < period:
        return None
    return max(highs[end - period : end]), min(lows[end - period : end])


def supertrend(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 10, mult: float = 3.0) -> tuple[int, float] | None:
    """Return (direction, line): direction 1 = bullish, -1 = bearish."""
    n = len(closes)
    if n < period + 2:
        return None
    tr = true_ranges(highs, lows, closes)
    atr_v = sum(tr[1 : period + 1]) / period
    atr_list = [atr_v]
    for i in range(period + 1, n):
        atr_v = (atr_v * (period - 1) + tr[i]) / period
        atr_list.append(atr_v)
    direction = 1
    final_upper = 0.0
    final_lower = 0.0
    line = 0.0
    for idx, i in enumerate(range(period, n)):
        a = atr_list[idx]
        hl2 = (highs[i] + lows[i]) / 2.0
        upper = hl2 + mult * a
        lower = hl2 - mult * a
        if idx == 0:
            final_upper, final_lower = upper, lower
            direction = 1 if closes[i] > upper else -1
            line = final_lower if direction == 1 else final_upper
            continue
        prev_close = closes[i - 1]
        final_upper = upper if (upper < final_upper or prev_close > final_upper) else final_upper
        final_lower = lower if (lower > final_lower or prev_close < final_lower) else final_lower
        if direction == -1 and closes[i] > final_upper:
            direction = 1
        elif direction == 1 and closes[i] < final_lower:
            direction = -1
        line = final_lower if direction == 1 else final_upper
    return direction, line


def pct_return(values: Sequence[float], bars: int) -> float | None:
    if len(values) <= bars or values[-1 - bars] <= 0:
        return None
    return (values[-1] / values[-1 - bars] - 1.0) * 100.0


def realized_vol_pct(values: Sequence[float], bars: int = 30) -> float | None:
    """Std-dev of simple bar returns (in %) over the last bars."""
    if len(values) < bars + 1:
        return None
    rets = [
        (values[i] / values[i - 1] - 1.0) * 100.0
        for i in range(len(values) - bars, len(values))
        if values[i - 1] > 0
    ]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return var ** 0.5


def volume_ratio(volumes: Sequence[float], lookback: int = 20) -> float | None:
    """Current bar volume divided by the average of the prior lookback bars."""
    if len(volumes) < lookback + 1:
        return None
    avg = sum(volumes[-lookback - 1 : -1]) / lookback
    if avg <= 0:
        return None
    return volumes[-1] / avg


def highest(values: Sequence[float], bars: int) -> float | None:
    if len(values) < bars or bars <= 0:
        return None
    return max(values[-bars:])


def lowest(values: Sequence[float], bars: int) -> float | None:
    if len(values) < bars or bars <= 0:
        return None
    return min(values[-bars:])


# ── Candle patterns ────────────────────────────────────────────────────────────

def is_hammer(o: float, h: float, lo: float, c: float) -> bool:
    body = abs(c - o)
    rng = h - lo
    if rng <= 0:
        return False
    lower_wick = min(o, c) - lo
    upper_wick = h - max(o, c)
    return body <= rng * 0.35 and lower_wick >= body * 2.0 and upper_wick <= body * 1.0


def is_bullish_engulfing(prev_o: float, prev_c: float, o: float, c: float) -> bool:
    return prev_c < prev_o and c > o and c >= prev_o and o <= prev_c
