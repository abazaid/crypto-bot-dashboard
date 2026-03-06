from statistics import mean, pstdev
from typing import List, Tuple


def ema(values: List[float], length: int) -> float:
    if not values:
        return 0.0
    k = 2 / (length + 1)
    current = values[0]
    for v in values[1:]:
        current = (v * k) + (current * (1 - k))
    return current


def rsi(values: List[float], length: int = 14) -> float:
    if len(values) < length + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    for i in range(length, len(gains)):
        avg_gain = ((avg_gain * (length - 1)) + gains[i]) / length
        avg_loss = ((avg_loss * (length - 1)) + losses[i]) / length
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def percent_change(series: List[float], periods: int) -> float:
    if len(series) <= periods or series[-periods - 1] == 0:
        return 0.0
    old = series[-periods - 1]
    new = series[-1]
    return ((new - old) / old) * 100


def bb_width(values: List[float], length: int = 20, k: float = 2.0) -> float:
    if len(values) < length:
        return 0.0
    window = values[-length:]
    mid = mean(window)
    sd = pstdev(window) if len(window) > 1 else 0.0
    upper = mid + (k * sd)
    lower = mid - (k * sd)
    if mid == 0:
        return 0.0
    return (upper - lower) / mid


def is_volume_accumulation(volumes: List[float]) -> bool:
    if len(volumes) < 60:
        return False
    recent = sum(volumes[-10:])
    baseline = mean(volumes[-60:-10]) * 10
    return recent > (baseline * 1.8)


def is_volatility_expanding(closes: List[float]) -> bool:
    if len(closes) < 60:
        return False
    current = bb_width(closes[-30:], 20)
    squeeze = min(bb_width(closes[i - 20 : i], 20) for i in range(30, len(closes) + 1))
    return squeeze > 0 and current > (squeeze * 1.35)


def relative_strength_ok(coin_closes_15m: List[float], btc_closes_15m: List[float]) -> bool:
    coin_change = percent_change(coin_closes_15m, 1)
    btc_change = percent_change(btc_closes_15m, 1)
    return coin_change > btc_change


def resistance_distance_ok(klines_15m: List[list], current_price: float, min_distance_pct: float = 2.0) -> bool:
    highs = [float(k[2]) for k in klines_15m[-60:]]
    higher_highs = [h for h in highs if h > current_price]
    if not higher_highs:
        return True
    next_resistance = min(higher_highs)
    distance_pct = ((next_resistance - current_price) / current_price) * 100
    return distance_pct > min_distance_pct


def trend_pullback_signal(klines_5m: List[list], klines_15m: List[list]) -> Tuple[bool, str]:
    ok, status, _ = trend_pullback_signal_with_checks(klines_5m, klines_15m)
    return ok, status


def trend_pullback_signal_with_checks(klines_5m: List[list], klines_15m: List[list]) -> Tuple[bool, str, dict]:
    closes_5m = [float(k[4]) for k in klines_5m]
    volumes_5m = [float(k[5]) for k in klines_5m]
    closes_15m = [float(k[4]) for k in klines_15m]
    if len(closes_5m) < 210 or len(closes_15m) < 210:
        return False, "No Data", {"data_ok": False}

    ema20_5m = ema(closes_5m[-120:], 20)
    ema50_5m = ema(closes_5m[-160:], 50)
    ema50_15m = ema(closes_15m[-160:], 50)
    ema200_15m = ema(closes_15m[-210:], 200)
    rsi_5m = rsi(closes_5m[-80:], 14)
    price = closes_5m[-1]
    volume = volumes_5m[-1]
    avg_volume = mean(volumes_5m[-20:]) if len(volumes_5m) >= 20 else volume

    trend_ok = ema50_15m > ema200_15m
    pullback_ok = abs(price - ema20_5m) / price <= 0.004 or abs(price - ema50_5m) / price <= 0.006
    rsi_ok = 40 <= rsi_5m <= 55
    volume_ok = volume > avg_volume * 1.5
    resistance_ok = resistance_distance_ok(klines_15m, price, 2.0)
    checks = {
        "data_ok": True,
        "trend_ok": trend_ok,
        "pullback_ok": pullback_ok,
        "rsi_ok": rsi_ok,
        "volume_spike_ok": volume_ok,
        "resistance_ok": resistance_ok,
        "rsi_value": rsi_5m,
        "volume_now": volume,
        "volume_avg20": avg_volume,
    }

    if trend_ok and pullback_ok and rsi_ok and volume_ok and resistance_ok:
        return True, "Buy Ready", checks
    if trend_ok:
        return False, "Watch", checks
    return False, "Blocked", checks
