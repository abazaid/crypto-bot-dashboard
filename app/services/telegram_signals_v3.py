"""
Parser for the "Shaban vip"-style bot posts (buy zone + T1..Tn + stop), spot only.

    🔵 VIRTUAL/USDT 🤖📈
    📍 Exchange: OKX
    ⚪️ Buy Zone: 0.7786 – 0.7500
    🟡 Targets: 🎯
    🟢 T1: 0.7919
    ...
    🟢 T5: 1.0121
    🔴 Stop Loss: 0.6827  اغلاق يوم

The channel names OKX; the user trades these on Binance only (listing is verified at ingest).
Futures posts ("FUTURES LONG 10X", "Xlm long 10x", "short") are NOT signals and return None, as are
progress posts ("#X/USDT Entered entry zone ✅", "Take-Profit target 2 ✅"). Targets carry no sell
percentages, so each gets an equal share. The stop is on a daily close in the channel; the bot
still uses its hard stop.
"""
from __future__ import annotations

import re

from app.services.telegram_signals import ParsedSignal, ParsedTarget, _clean, _numbers

_SYMBOL_RE = re.compile(r"([A-Za-z0-9]{2,15})\s*/\s*USDT", re.IGNORECASE)
_ZONE_RE = re.compile(r"Buy\s*Zone\s*:\s*(.+)", re.IGNORECASE)
_TARGET_RE = re.compile(r"\bT(\d+)\s*:\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_STOP_RE = re.compile(r"Stop\s*Loss\s*:\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_FUTURES_RE = re.compile(r"\b(long|short)\b|\b\d+\s*x\b|futures", re.IGNORECASE)


def looks_like_signal_v3(text: str) -> bool:
    t = text or ""
    return bool(_ZONE_RE.search(t) and _TARGET_RE.search(t) and _STOP_RE.search(t)) and not is_futures(t)


def is_futures(text: str) -> bool:
    """Leverage / long / short posts are futures. "Future or spot" posts stay tradable as spot."""
    t = text or ""
    if re.search(r"future\s+or\s+spot", t, re.IGNORECASE):
        t = re.sub(r"future\s+or\s+spot", "", t, flags=re.IGNORECASE)
    return bool(_FUTURES_RE.search(t))


def parse_signal_v3(text: str) -> ParsedSignal | None:
    if not looks_like_signal_v3(text or ""):
        return None
    lines = [ln for ln in (_clean(x) for x in (text or "").splitlines()) if ln]
    body = "\n".join(lines)

    sm = _SYMBOL_RE.search(body)
    zm = _ZONE_RE.search(body)
    stm = _STOP_RE.search(body)
    if not (sm and zm and stm):
        return None
    base = sm.group(1).upper()
    zone = _numbers(zm.group(1))[:2]
    if not zone:
        return None
    entry_low, entry_high = min(zone), max(zone)
    stop = float(stm.group(1).replace(",", "."))

    by_index: dict[int, float] = {}
    for ln in lines:
        for m in _TARGET_RE.finditer(ln):
            by_index.setdefault(int(m.group(1)), float(m.group(2).replace(",", ".")))
    if not by_index:
        return None
    targets = [ParsedTarget(price=p, pct=None, sell_fraction=0.0) for p in sorted(by_index.values())]
    n = len(targets)
    for t in targets:
        t.sell_fraction = 1.0 / n
    targets[-1].sell_fraction = max(0.0, 1.0 - sum(t.sell_fraction for t in targets[:-1]))

    warnings: list[str] = []
    if re.search(r"اغلاق|إغلاق", body):
        warnings.append("channel stop is on a daily close; the bot uses a hard stop")
    return ParsedSignal(
        symbol=f"{base}USDT",
        base=base,
        exchange="binance",  # the channel names OKX; we trade Binance spot only
        entry_low=float(entry_low),
        entry_high=float(entry_high),
        stop_price=stop,
        stop_on_4h_close=False,
        targets=targets,
        warnings=warnings,
    )
