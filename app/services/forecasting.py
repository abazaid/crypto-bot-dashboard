from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.paper_v2 import AIForecastCache
from app.services.binance_public import get_klines


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    p = max(2, int(period))
    alpha = 2.0 / (p + 1.0)
    out = float(values[0])
    for v in values[1:]:
        out = (alpha * float(v)) + ((1.0 - alpha) * out)
    return out


def _rsi(values: list[float], period: int = 14) -> float:
    p = max(2, int(period))
    if len(values) < p + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(values)):
        d = float(values[i]) - float(values[i - 1])
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:p]) / p
    avg_loss = sum(losses[:p]) / p
    for i in range(p, len(gains)):
        avg_gain = ((avg_gain * (p - 1)) + gains[i]) / p
        avg_loss = ((avg_loss * (p - 1)) + losses[i]) / p
    if avg_loss <= 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr_pct(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    p = max(2, int(period))
    if len(closes) < p + 2:
        return 2.0
    trs: list[float] = []
    for i in range(1, len(closes)):
        h = float(highs[i])
        l = float(lows[i])
        pc = float(closes[i - 1])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    atr = sum(trs[:p]) / p
    for i in range(p, len(trs)):
        atr = ((atr * (p - 1)) + trs[i]) / p
    price = max(1e-12, float(closes[-1]))
    return (atr / price) * 100.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _compute_forecast(symbol: str, interval: str, horizon_days: int, klines_limit: int) -> dict:
    raw = get_klines(symbol, interval=interval, limit=max(120, int(klines_limit)))
    if not raw or len(raw) < 60:
        raise RuntimeError(f"not_enough_data_for_{symbol}")

    closes = [float(x[4]) for x in raw]
    highs = [float(x[2]) for x in raw]
    lows = [float(x[3]) for x in raw]
    vols = [float(x[5]) for x in raw]
    price = float(closes[-1])
    ema50 = _ema(closes[-120:], 50)
    ema200 = _ema(closes[-220:] if len(closes) >= 220 else closes, 200)
    rsi14 = _rsi(closes[-200:], 14)
    atr_pct = _atr_pct(highs[-220:], lows[-220:], closes[-220:], 14)
    vol_avg20 = (sum(vols[-20:]) / 20.0) if len(vols) >= 20 else (sum(vols) / max(len(vols), 1))
    vol_ratio = (vols[-1] / vol_avg20) if vol_avg20 > 1e-12 else 1.0

    bias_score = 0
    bias_score += 2 if price > ema200 else -2
    bias_score += 1 if ema50 > ema200 else -1
    if rsi14 > 55:
        bias_score += 1
    elif rsi14 < 45:
        bias_score -= 1

    if bias_score >= 2:
        bias = "bullish"
    elif bias_score <= -2:
        bias = "bearish"
    else:
        bias = "neutral"

    base_move = max(0.4, atr_pct * math.sqrt(max(1.0, float(horizon_days))))
    if bias == "bullish":
        expected_move_pct = base_move * 1.15
    elif bias == "bearish":
        expected_move_pct = -base_move * 1.15
    else:
        expected_move_pct = base_move * 0.35 if rsi14 >= 50 else -base_move * 0.35

    conf = 50.0
    if abs((price - ema200) / max(ema200, 1e-12)) >= 0.02:
        conf += 10.0
    if (bias == "bullish" and rsi14 > 52) or (bias == "bearish" and rsi14 < 48):
        conf += 5.0
    if vol_ratio >= 1.2:
        conf += 5.0
    if atr_pct >= 4.0:
        conf -= 8.0
    confidence_pct = _clamp(conf, 40.0, 75.0)

    if atr_pct < 1.5:
        volatility_level = "low"
    elif atr_pct < 3.5:
        volatility_level = "medium"
    else:
        volatility_level = "high"

    forecast_delta_usdt = price * (expected_move_pct / 100.0)
    direction = "up" if expected_move_pct > 0 else ("down" if expected_move_pct < 0 else "flat")
    sign = "+" if expected_move_pct >= 0 else ""
    tooltip = (
        f"AI predicts {direction} move of {sign}{expected_move_pct:.2f}% "
        f"({sign}{forecast_delta_usdt:.2f} USDT) in {horizon_days}d.\n"
        f"Confidence: {confidence_pct:.0f}% | Bias: {bias} | Volatility: {volatility_level}\n"
        f"EMA50={ema50:.4f}, EMA200={ema200:.4f}, RSI={rsi14:.1f}, ATR%={atr_pct:.2f}, VolRatio={vol_ratio:.2f}"
    )

    return {
        "symbol": symbol.upper(),
        "interval": interval,
        "horizon_days": int(horizon_days),
        "price": price,
        "expected_move_pct": expected_move_pct,
        "confidence_pct": confidence_pct,
        "bias": bias,
        "volatility_level": volatility_level,
        "direction": direction,
        "arrow": "↑" if expected_move_pct > 0 else ("↓" if expected_move_pct < 0 else "→"),
        "tooltip": tooltip,
        "details": {
            "ema50": ema50,
            "ema200": ema200,
            "rsi14": rsi14,
            "atr_pct": atr_pct,
            "vol_ratio": vol_ratio,
            "forecast_delta_usdt": forecast_delta_usdt,
        },
    }


def get_or_build_forecast(
    db: Session,
    symbol: str,
    force_refresh: bool = False,
    interval: str | None = None,
    horizon_days: int | None = None,
) -> dict | None:
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None
    iv = str(interval or "4h")
    hd = int(horizon_days or 7)
    ttl_seconds = max(300, int(getattr(settings, "slow_recalc_seconds", 14400)))
    now = datetime.utcnow()

    row = db.query(AIForecastCache).filter(AIForecastCache.symbol == sym).first()
    if row and (not force_refresh):
        if row.expires_at and row.expires_at > now:
            d = row.details()
            expected_move_pct = float(row.expected_move_pct or 0.0)
            return {
                "symbol": sym,
                "interval": row.interval,
                "horizon_days": int(row.horizon_days or hd),
                "price": float(row.as_of_price or 0.0),
                "expected_move_pct": expected_move_pct,
                "confidence_pct": float(row.confidence_pct or 50.0),
                "bias": str(row.bias or "neutral"),
                "volatility_level": str(row.volatility_level or "medium"),
                "direction": "up" if expected_move_pct > 0 else ("down" if expected_move_pct < 0 else "flat"),
                "arrow": "↑" if expected_move_pct > 0 else ("↓" if expected_move_pct < 0 else "→"),
                "tooltip": str(d.get("tooltip", "")) or "Cached forecast",
                "details": d,
                "cached": True,
                "updated_at": row.updated_at,
            }

    fc = _compute_forecast(sym, iv, hd, klines_limit=300)
    expires_at = now + timedelta(seconds=ttl_seconds)
    if not row:
        row = AIForecastCache(symbol=sym)
        db.add(row)
    row.interval = iv
    row.horizon_days = hd
    row.expected_move_pct = float(fc["expected_move_pct"])
    row.confidence_pct = float(fc["confidence_pct"])
    row.bias = str(fc["bias"])
    row.volatility_level = str(fc["volatility_level"])
    row.as_of_price = float(fc["price"])
    row.details_json = json.dumps({"tooltip": fc["tooltip"], **(fc.get("details") or {})})
    row.expires_at = expires_at
    db.flush()
    fc["cached"] = False
    fc["updated_at"] = row.updated_at
    return fc


def get_forecasts_for_symbols(db: Session, symbols: list[str], build_limit: int = 8) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not symbols:
        return out
    pending: list[str] = []
    for s in sorted(set(str(x).upper().strip() for x in symbols if x)):
        row = db.query(AIForecastCache).filter(AIForecastCache.symbol == s).first()
        if row and row.expires_at and row.expires_at > datetime.utcnow():
            expected_move_pct = float(row.expected_move_pct or 0.0)
            details = row.details()
            out[s] = {
                "symbol": s,
                "expected_move_pct": expected_move_pct,
                "confidence_pct": float(row.confidence_pct or 50.0),
                "bias": str(row.bias or "neutral"),
                "volatility_level": str(row.volatility_level or "medium"),
                "direction": "up" if expected_move_pct > 0 else ("down" if expected_move_pct < 0 else "flat"),
                "arrow": "↑" if expected_move_pct > 0 else ("↓" if expected_move_pct < 0 else "→"),
                "tooltip": str(details.get("tooltip", "")) or "Cached forecast",
                "cached": True,
            }
            continue
        pending.append(s)

    batch = pending[: max(0, int(build_limit))]
    iv = str(getattr(settings, "forecast_interval", None) or "4h")
    hd = int(getattr(settings, "forecast_horizon_days", None) or 7)
    ttl_seconds = max(300, int(getattr(settings, "slow_recalc_seconds", 14400)))
    now = datetime.utcnow()
    expires_at = now + timedelta(seconds=ttl_seconds)

    # Parallelize only the Binance API calls (_compute_forecast); DB writes stay sequential.
    computed: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(len(batch), 6)) as ex:
        fut_to_sym = {ex.submit(_compute_forecast, s, iv, hd, 300): s for s in batch}
        for fut in as_completed(fut_to_sym):
            s = fut_to_sym[fut]
            try:
                computed[s] = fut.result()
            except Exception:
                continue

    for s, fc in computed.items():
        try:
            row = db.query(AIForecastCache).filter(AIForecastCache.symbol == s).first()
            if not row:
                row = AIForecastCache(symbol=s)
                db.add(row)
            row.interval = iv
            row.horizon_days = hd
            row.expected_move_pct = float(fc["expected_move_pct"])
            row.confidence_pct = float(fc["confidence_pct"])
            row.bias = str(fc["bias"])
            row.volatility_level = str(fc["volatility_level"])
            row.as_of_price = float(fc["price"])
            row.details_json = json.dumps({"tooltip": fc["tooltip"], **(fc.get("details") or {})})
            row.expires_at = expires_at
            db.flush()
            fc["cached"] = False
            out[s] = fc
        except Exception:
            continue
    return out

