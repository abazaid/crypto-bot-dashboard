"""
AI Trader strategy engine — long-only spot signals built from an ensemble of
the approaches that hold up best in crypto backtests and academic research:

  * Donchian breakout (20-bar, 4h) with ADX + volume confirmation
  * Trend pullback (4h EMA stack, 1h RSI/MACD trigger, candle confirmation)
  * Bollinger squeeze breakout (4h)

All three are gated by a BTC market-regime filter, a time-series-momentum
filter (30d / 7d returns) and a volatility sanity band. Position sizing is
volatility-based (risk per trade / ATR stop distance) and is done by the pool
service, not here.

This module is deterministic: given klines it returns the same signal. It has
no exchange side effects beyond reading public market data.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from app.core.config import settings
from app.services.ai_indicators import (
    adx,
    atr,
    bollinger,
    bollinger_bandwidth_series,
    donchian,
    ema,
    is_bullish_engulfing,
    is_hammer,
    lowest,
    macd_hist_series,
    parse_klines,
    pct_return,
    realized_vol_pct,
    rsi,
    volume_ratio,
)
from app.services.analytics import btc_market_state, is_loop_excluded_symbol
from app.services.binance_public import get_24h_tickers, get_klines

logger = logging.getLogger(__name__)

MIN_QUOTE_VOLUME_USDT = 10_000_000.0
UNIVERSE_SIZE = 40
SCAN_WORKERS = 8
BLOCKED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
STABLE_BASES = {"USDT", "USDC", "BUSD", "TUSD", "FDUSD", "USDE", "USDD", "USDP", "DAI", "USD1", "PYUSD", "RLUSD", "USDS", "EURI", "EUR", "PAXG", "XAUT", "WBTC", "WBETH"}
# Tokenized stocks / ETFs trade only during exchange hours: gaps break 24/7 ATR stops. Extend via AI_EXCLUDED_BASES.
TOKENIZED_STOCK_BASES = {"CRCLB", "SNDKB", "TSLAB", "NVDAB", "AAPLB", "MSTRB", "COINB", "SPYB", "QQQB", "GOOGLB", "AMZNB", "METAB", "HOODB"}
_ENV_EXCLUDED_BASES = {x.strip().upper() for x in os.getenv("AI_EXCLUDED_BASES", "").split(",") if x.strip()}

_INTERVAL_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400}
_KLINE_CACHE: dict[tuple[str, str], dict] = {}
_KLINE_LOCK = threading.Lock()
_REGIME_CACHE: dict = {"expires_at": 0.0, "state": "neutral"}
_BIAS_CACHE: dict = {"expires_at": 0.0, "state": "ok"}


@dataclass
class Signal:
    symbol: str
    strategy: str
    score: float
    price: float
    atr_4h: float
    stop_price: float
    tp1_price: float
    tp1_fraction: float
    trail_mult: float
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def risk_pct(self) -> float:
        """Stop distance as a percentage of entry price."""
        if self.price <= 0:
            return 0.0
        return (self.price - self.stop_price) / self.price * 100.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "score": round(self.score, 1),
            "price": self.price,
            "stop_price": self.stop_price,
            "tp1_price": self.tp1_price,
            "risk_pct": round(self.risk_pct, 2),
            "trail_mult": self.trail_mult,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
        }


# ── Market data (cached) ───────────────────────────────────────────────────────

def get_klines_cached(symbol: str, interval: str, limit: int) -> list[list]:
    """Fetch klines with a cache that lasts a quarter of the bar length (min 60s)."""
    key = (symbol.upper(), interval)
    now = time.time()
    with _KLINE_LOCK:
        entry = _KLINE_CACHE.get(key)
        if entry and entry["expires_at"] > now and len(entry["data"]) >= limit:
            return entry["data"][-limit:]
    data = get_klines(symbol, interval, limit)
    ttl = max(60, _INTERVAL_SECONDS.get(interval, 3600) // 4)
    with _KLINE_LOCK:
        _KLINE_CACHE[key] = {"expires_at": now + ttl, "data": data}
    return data


def market_regime(force_refresh: bool = False) -> str:
    """BTC regime: bullish | neutral | bearish | strong_bearish (cached 15 min)."""
    now = time.time()
    if not force_refresh and _REGIME_CACHE["expires_at"] > now:
        return str(_REGIME_CACHE["state"])
    try:
        state = btc_market_state()
    except Exception as exc:  # network / parsing errors -> stay defensive
        logger.warning("AI regime check failed: %s", exc)
        state = str(_REGIME_CACHE.get("state") or "neutral")
    _REGIME_CACHE["state"] = state
    _REGIME_CACHE["expires_at"] = now + 900
    return state


def btc_short_term_bias(force_refresh: bool = False) -> str:
    """
    'weak' when BTC is under its 1h EMA20 or fell more than 1% over the last 4 hours, else 'ok'.
    Altcoin longs opened while BTC is weak intraday get half the normal risk budget. Cached 5 min.
    """
    now = time.time()
    if not force_refresh and _BIAS_CACHE["expires_at"] > now:
        return str(_BIAS_CACHE["state"])
    state = str(_BIAS_CACHE.get("state") or "ok")
    try:
        kl = get_klines_cached("BTCUSDT", "1h", 60)
        closes = [float(k[4]) for k in kl[:-1]]
        e20 = ema(closes, 20)
        r4 = pct_return(closes, 4)
        if e20 is not None and r4 is not None:
            state = "weak" if (closes[-1] < e20 or r4 < -1.0) else "ok"
    except Exception as exc:
        logger.warning("AI BTC bias check failed: %s", exc)
    _BIAS_CACHE["state"] = state
    _BIAS_CACHE["expires_at"] = now + 300
    return state


def is_tradeable_symbol(symbol: str) -> bool:
    s = (symbol or "").upper().strip()
    if not s.endswith("USDT") or len(s) < 6:
        return False
    if any(s.endswith(x) for x in BLOCKED_SUFFIXES):
        return False
    base = s[:-4]
    if base in STABLE_BASES or base in TOKENIZED_STOCK_BASES or base in _ENV_EXCLUDED_BASES:
        return False
    if is_loop_excluded_symbol(s):
        return False
    return True


def build_universe(max_symbols: int = UNIVERSE_SIZE, exclude: set[str] | None = None) -> list[dict]:
    """Top USDT pairs by 24h quote volume, excluding leveraged tokens, stables and excluded symbols."""
    excluded = {s.upper() for s in (exclude or set())}
    rows: list[dict] = []
    for row in get_24h_tickers():
        symbol = str(row.get("symbol", "")).upper()
        if symbol in excluded or not is_tradeable_symbol(symbol):
            continue
        try:
            qv = float(row.get("quoteVolume", 0.0) or 0.0)
            chg = float(row.get("priceChangePercent", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if qv < MIN_QUOTE_VOLUME_USDT:
            continue
        rows.append({"symbol": symbol, "quote_volume": qv, "change_24h": chg})
    rows.sort(key=lambda r: r["quote_volume"], reverse=True)
    return rows[: max(1, int(max_symbols))]


# ── Signal evaluation ──────────────────────────────────────────────────────────

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def evaluate_symbol(symbol: str, regime: str, kl4h: list[list] | None = None, kl1h: list[list] | None = None, kl1d: list[list] | None = None) -> Signal | None:
    """
    Evaluate one symbol and return the best signal across the strategy ensemble,
    or None when no strategy qualifies. Klines may be injected (tests / batch).
    """
    if regime == "strong_bearish":
        return None
    try:
        kl4h = kl4h if kl4h is not None else get_klines_cached(symbol, "4h", 230)
        kl1h = kl1h if kl1h is not None else get_klines_cached(symbol, "1h", 160)
        kl1d = kl1d if kl1d is not None else get_klines_cached(symbol, "1d", 70)
    except Exception as exc:
        logger.debug("AI klines failed for %s: %s", symbol, exc)
        return None
    if len(kl4h) < 215 or len(kl1h) < 120 or len(kl1d) < 40:
        return None

    # Work on CLOSED bars for signals; the forming bar is only used as the live price.
    c4 = parse_klines(kl4h[:-1])
    c1 = parse_klines(kl1h[:-1])
    cd = parse_klines(kl1d[:-1])
    live_price = float(kl4h[-1][4])
    if live_price <= 0:
        return None
    # Markets that pause (tokenized stocks, halted pairs) show zero-volume hours; a 24/7 stop engine must skip them.
    if any(v <= 0 for v in c1.volumes[-48:]) or any(v <= 0 for v in c4.volumes[-30:]):
        return None

    ema20_4 = ema(c4.closes, 20)
    ema50_4 = ema(c4.closes, 50)
    ema200_4 = ema(c4.closes, 200)
    atr_4 = atr(c4.highs, c4.lows, c4.closes, 14)
    adx_4 = adx(c4.highs, c4.lows, c4.closes, 14)
    rsi_4 = rsi(c4.closes, 14)
    vol_ratio_4 = volume_ratio(c4.volumes, 20)
    ema50_d = ema(cd.closes, 50)
    ret_30d = pct_return(cd.closes, 30)
    ret_7d = pct_return(cd.closes, 7)
    vol_30d = realized_vol_pct(cd.closes, 30)
    if None in (ema20_4, ema50_4, ema200_4, atr_4, adx_4, rsi_4, vol_ratio_4, ema50_d, ret_30d, ret_7d):
        return None
    assert atr_4 is not None and ema20_4 is not None and ema50_4 is not None and ema200_4 is not None
    assert adx_4 is not None and rsi_4 is not None and vol_ratio_4 is not None and ema50_d is not None
    assert ret_30d is not None and ret_7d is not None

    close_4 = c4.closes[-1]
    atr_pct = atr_4 / close_4 * 100.0
    # Volatility sanity band: dead coins and blow-off coins are both bad for ATR stops.
    if atr_pct < 0.6 or atr_pct > 9.0:
        return None
    # The setup is judged on the last CLOSED bar but we enter at the live price:
    # skip if price already ran away from the setup (chasing) or fell back through it.
    if live_price > close_4 + 0.75 * atr_4 or live_price < close_4 - 1.0 * atr_4:
        return None

    trend_ok = ema50_4 > ema200_4 or cd.closes[-1] > ema50_d
    strong_stack = ema20_4 > ema50_4 > ema200_4
    tsmom_ok = ret_30d > 0
    risk_adj_mom = (ret_7d / vol_30d) if vol_30d and vol_30d > 0 else 0.0

    base_metrics = {
        "atr_pct": round(atr_pct, 2),
        "adx": round(adx_4, 1),
        "rsi_4h": round(rsi_4, 1),
        "vol_ratio": round(vol_ratio_4, 2),
        "ret_30d": round(ret_30d, 1),
        "ret_7d": round(ret_7d, 1),
        "ema_stack": strong_stack,
        "above_ema200": close_4 > ema200_4,
        "risk_adj_mom": round(risk_adj_mom, 2),
        "regime": regime,
    }

    candidates: list[Signal] = []

    # ── Strategy A: Donchian breakout (4h) ───────────────────────────────
    dc = donchian(c4.highs, c4.lows, 20, exclude_last=True)
    if dc and trend_ok and tsmom_ok:
        upper, _lower = dc
        extension = (live_price - upper) / atr_4 if atr_4 > 0 else 99.0
        vol_needed = 1.8 if regime == "bearish" else 1.3
        if close_4 > upper and 0.0 <= extension <= 1.5 and adx_4 >= 20 and vol_ratio_4 >= vol_needed and rsi_4 < 78:
            stop = live_price - 2.0 * atr_4
            dc10 = donchian(c4.highs, c4.lows, 10, exclude_last=True)
            if dc10:
                stop = max(stop, dc10[1])
            stop = min(stop, live_price - 1.0 * atr_4)  # never tighter than 1 ATR
            r = live_price - stop
            score = 55.0
            score += min(10.0, max(0.0, adx_4 - 20.0))
            score += min(10.0, max(0.0, (vol_ratio_4 - 1.0) * 8.0))
            score += 6.0 if strong_stack else 0.0
            score += 5.0 if ret_7d > 0 else 0.0
            score += 4.0 if cd.closes[-1] > ema50_d else 0.0
            score -= 8.0 if atr_pct > 6.0 else 0.0
            score -= 8.0 if rsi_4 > 70.0 else 0.0  # breaking out already overbought on 4h
            score -= 5.0 if ret_7d > 15.0 else 0.0  # late in the move
            score -= 5.0 if ret_7d > 30.0 else 0.0  # parabolic already
            score += 5.0 if regime == "bullish" else (-10.0 if regime == "bearish" else 0.0)
            reasons = [
                f"4h close broke 20-bar Donchian high ({upper:.6g})",
                f"ADX {adx_4:.0f} confirms trend strength",
                f"volume x{vol_ratio_4:.1f} vs 20-bar avg",
                "30d momentum positive",
            ]
            candidates.append(Signal(symbol, "breakout", _clamp(score, 0, 100), live_price, atr_4, stop, live_price + 1.5 * r, 0.4, 3.0, reasons, dict(base_metrics, donchian_upper=upper)))

    # ── Strategy B: Trend pullback (4h trend, 1h trigger) ────────────────
    if strong_stack and close_4 > ema50_4 - 0.3 * atr_4 and tsmom_ok:
        recent_lows = c4.lows[-5:]
        touched = any(lo <= ema20_4 + 0.5 * atr_4 for lo in recent_lows)
        rsi_1 = rsi(c1.closes, 14)
        rsi_1_prev = rsi(c1.closes[:-3], 14)
        hist = macd_hist_series(c1.closes)
        # Histogram improving on the last closed 1h bar, or a fresh cross above zero.
        macd_turning = len(hist) >= 3 and (hist[-1] > hist[-2] or (hist[-2] <= 0 < hist[-1]))
        o, h, lo, c = c1.opens[-1], c1.highs[-1], c1.lows[-1], c1.closes[-1]
        po, pc = c1.opens[-2], c1.closes[-2]
        candle_ok = c > o or is_hammer(o, h, lo, c) or is_bullish_engulfing(po, pc, o, c)
        if (
            touched
            and rsi_1 is not None
            and rsi_1_prev is not None
            and 35.0 <= rsi_1 <= 62.0
            and rsi_1 > rsi_1_prev
            and rsi_4 < 72.0
            and macd_turning
            and candle_ok
        ):
            swing_low = lowest(c4.lows, 5) or (live_price - 1.5 * atr_4)
            stop = min(swing_low - 0.2 * atr_4, live_price - 1.5 * atr_4)
            stop = max(stop, live_price - 2.5 * atr_4)  # cap the risk
            r = live_price - stop
            # Recalibrated: pullbacks used to score 85-95 across the board, which made the score
            # useless for ranking. Base lowered and penalties added for extended/overbought entries.
            score = 48.0
            score += 8.0 if adx_4 >= 20 else (-5.0 if adx_4 < 18 else 0.0)
            score += 6.0 if cd.closes[-1] > ema50_d else 0.0
            score += 5.0 if ret_7d > -3.0 else 0.0
            score += min(8.0, max(0.0, (rsi_1 - 35.0) / 27.0 * 8.0))
            score += 5.0 if vol_ratio_4 >= 1.0 else 0.0
            score -= 8.0 if atr_pct > 6.0 else 0.0
            score -= 6.0 if rsi_4 > 65.0 else 0.0  # 4h already overbought
            score -= 6.0 if (close_4 - ema20_4) > 1.0 * atr_4 else 0.0  # bounce already extended past EMA20
            score -= 6.0 if ret_7d > 25.0 else 0.0  # chasing a parabolic week
            # Depth of the pullback: touching EMA50 is a better entry than barely grazing EMA20.
            deepest = min(c4.lows[-5:])
            score += 6.0 if deepest <= ema50_4 + 0.3 * atr_4 else 0.0
            score += 5.0 if regime == "bullish" else (-12.0 if regime == "bearish" else 0.0)
            reasons = [
                "4h EMA20 > EMA50 > EMA200 uptrend",
                f"pulled back to EMA20 zone within last 5 bars",
                f"1h RSI {rsi_1:.0f} rising, MACD histogram turning up",
                "bullish 1h confirmation candle",
            ]
            candidates.append(Signal(symbol, "pullback", _clamp(score, 0, 100), live_price, atr_4, stop, live_price + 1.5 * r, 0.5, 2.0, reasons, dict(base_metrics, rsi_1h=round(rsi_1, 1))))

    # ── Strategy C: Bollinger squeeze breakout (4h) ──────────────────────
    bw = bollinger_bandwidth_series(c4.closes, 20, 2.0)
    bb = bollinger(c4.closes, 20, 2.0)
    if bb and len(bw) >= 100 and trend_ok and tsmom_ok:
        upper_b, mid_b, _lower_b = bb
        window = sorted(bw[-100:])
        p25 = window[int(len(window) * 0.25)]
        squeeze_recent = any(v <= p25 for v in bw[-4:-1]) or bw[-1] <= p25
        vol_needed = 2.0 if regime == "bearish" else 1.5
        if squeeze_recent and close_4 > upper_b and vol_ratio_4 >= vol_needed and rsi_4 < 78 and close_4 > ema50_4:
            stop = max(mid_b, live_price - 2.0 * atr_4)
            stop = min(stop, live_price - 1.0 * atr_4)
            r = live_price - stop
            score = 54.0
            score += min(12.0, max(0.0, (vol_ratio_4 - 1.0) * 8.0))
            score += 8.0 if strong_stack else 0.0
            score += 6.0 if adx_4 >= 18 else 0.0
            score += 5.0 if ret_7d > 0 else 0.0
            score -= 8.0 if atr_pct > 6.0 else 0.0
            score += 5.0 if regime == "bullish" else (-10.0 if regime == "bearish" else 0.0)
            reasons = [
                "Bollinger bandwidth in bottom quartile of last 100 bars (squeeze)",
                f"4h close above upper band ({upper_b:.6g}) on volume x{vol_ratio_4:.1f}",
                "trend filter passed",
            ]
            candidates.append(Signal(symbol, "squeeze", _clamp(score, 0, 100), live_price, atr_4, stop, live_price + 1.5 * r, 0.4, 2.5, reasons, dict(base_metrics, bandwidth=round(bw[-1], 4))))

    if not candidates:
        return None
    best = max(candidates, key=lambda s: s.score)
    # Sanity: never accept a stop above the live price or a risk under 0.8%.
    if best.stop_price >= live_price * 0.995 or best.risk_pct < 0.8:
        return None
    # Fee-aware: reward-to-risk on TP1 must exceed 1.2 after round-trip fees.
    fee_rt = 2.0 * float(settings.trading_fee_pct) / 100.0
    reward = (best.tp1_price - live_price) / live_price - fee_rt
    risk = (live_price - best.stop_price) / live_price
    if risk <= 0 or reward / risk < 1.2:
        return None
    return best


def regime_min_score_adjust(regime: str) -> float:
    return {"bullish": 0.0, "neutral": 5.0, "bearish": 12.0}.get(regime, 0.0)


def scan_universe(regime: str, exclude: set[str] | None = None, max_symbols: int = UNIVERSE_SIZE) -> tuple[list[Signal], list[dict]]:
    """
    Evaluate the liquid universe and return (signals sorted best-first, scanned rows).
    scanned rows are for the UI so the user can see why nothing was bought.
    """
    universe = build_universe(max_symbols=max_symbols, exclude=exclude)
    signals: list[Signal] = []
    scanned: list[dict] = []

    def _eval(symbol: str) -> tuple[str, Signal | None]:
        try:
            return symbol, evaluate_symbol(symbol, regime)
        except Exception as exc:  # one bad symbol must not abort the whole scan
            logger.warning("AI scan: %s failed: %s", symbol, exc)
            return symbol, None

    # Binance public klines are cheap (weight 2); 8 workers keeps a 40-symbol scan around 20s.
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix="ai-scan") as pool:
        results = list(pool.map(_eval, [row["symbol"] for row in universe]))
    for symbol, sig in results:
        if sig:
            signals.append(sig)
            scanned.append({"symbol": symbol, "result": "signal", "strategy": sig.strategy, "score": round(sig.score, 1)})
        else:
            scanned.append({"symbol": symbol, "result": "no_setup", "strategy": None, "score": None})
    signals.sort(key=lambda s: (s.score, s.metrics.get("risk_adj_mom", 0.0)), reverse=True)
    return signals, scanned
