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

Since 2026-10-02 the channel also posts a shorter layout, parsed the same way:

    💎 #ZEN | OKX spot
    📍 Entry: 6.700 6.400
    🎯 Targets:
    1️⃣ 7.275
    2️⃣ 8.400
    3️⃣ 9.600
    🛑5.760 CLOSE 1D

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
_HASH_SYMBOL_RE = re.compile(r"#([A-Za-z0-9]{1,15})\s*\|")  # "💎 #ZEN | OKX spot"
_ZONE_RE = re.compile(r"(?:Buy\s*Zone|Entry|منطقة الدخول)\s*:\s*(.*)", re.IGNORECASE)
_TARGET_RE = re.compile(r"\bT(\d+)\s*:\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_KEYCAP_TARGET_RE = re.compile(r"^(\d{1,2})️?⃣\s*(\d+(?:[.,]\d+)?)\s*✅?\s*$")  # "1️⃣ 7.275", "1️⃣ 2.20✅"
_STOP_RE = re.compile(r"(?:Stop\s*Loss\s*:|وقف الخسارة\s*:|🛑)\s*(.*)", re.IGNORECASE)
_CLOSED_RE = re.compile(r"🏁\s*Closed|تم الإغلاق|الحالة:\s*مغلقة")


def _value_after(lines: list[str], rx: re.Pattern) -> str | None:
    """Text after the label on the same line, or the next line when the label stands alone ("منطقة الدخول:\\n2.022 – 2.076")."""
    for i, ln in enumerate(lines):
        m = rx.search(ln)
        if not m:
            continue
        rest = m.group(1).strip()
        if not re.search(r"\d", rest) and i + 1 < len(lines):
            rest = lines[i + 1]
        return rest if re.search(r"\d", rest) else None
    return None


def _stop_level(text: str) -> float | None:
    """"5.760 CLOSE 1D" -> 5.76; "إغلاق شمعة 4 ساعات أسفل 1.87" -> 1.87 (the level after أسفل, not the 4 hours)."""
    if "أسفل" in text:
        nums = _numbers(text.split("أسفل", 1)[1])
    else:
        nums = _numbers(text)
    return nums[0] if nums else None


_FUTURES_RE = re.compile(r"\b(long|short)\b|\b\d+\s*x\b|futures", re.IGNORECASE)


def looks_like_signal_v3(text: str) -> bool:
    t = text or ""
    if _CLOSED_RE.search(t) or is_futures(t):
        return False
    lines = [ln for ln in (_clean(x) for x in t.splitlines()) if ln]
    has_targets = bool(_TARGET_RE.search(t)) or any(_KEYCAP_TARGET_RE.match(ln) for ln in lines)
    return bool(has_targets and _value_after(lines, _ZONE_RE) and _value_after(lines, _STOP_RE))


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

    sm = _SYMBOL_RE.search(body) or _HASH_SYMBOL_RE.search(body)
    zone_text = _value_after(lines, _ZONE_RE)
    stop_text = _value_after(lines, _STOP_RE)
    if not (sm and zone_text and stop_text):
        return None
    base = sm.group(1).upper()
    zone = _numbers(zone_text)[:2]
    stop = _stop_level(stop_text)
    if not zone or stop is None:
        return None
    entry_low, entry_high = min(zone), max(zone)

    by_index: dict[int, float] = {}
    for ln in lines:
        for m in _TARGET_RE.finditer(ln):
            by_index.setdefault(int(m.group(1)), float(m.group(2).replace(",", ".")))
        km = _KEYCAP_TARGET_RE.match(ln)
        if km:
            by_index.setdefault(int(km.group(1)), float(km.group(2).replace(",", ".")))
    if not by_index:
        return None
    targets = [ParsedTarget(price=p, pct=None, sell_fraction=0.0) for p in sorted(by_index.values())]
    n = len(targets)
    for t in targets:
        t.sell_fraction = 1.0 / n
    targets[-1].sell_fraction = max(0.0, 1.0 - sum(t.sell_fraction for t in targets[:-1]))

    warnings: list[str] = []
    if re.search(r"اغلاق|إغلاق|close\s*1d", stop_text, re.IGNORECASE):
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
