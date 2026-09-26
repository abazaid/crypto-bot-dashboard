"""
Parser for the "0x3rwah.eth"-style posts (market entry + optional second entry + N targets + stop).

    DODO
    🟢 دخول فوري
    0.0184 $
    🟡 دخول ثاني (-5.0%)
    0.0175 $
    🎯هدف 1: 0.0192  (+4.0%)
    ...
    🎯هدف 5: 0.0211  (+14.4%)
    ⛔️وقف: 0.0168  (-4.0% تحت آخر دخول)

Optional lines: "⚠️ خطورة متوسطة" / "🚨 خطورة عالية". Some posts have no second entry.
Progress updates ("AXL 🎯 هدف 1 ✅"), averaging notices ("متوسط الدخول"), level posts
("سعر المستوى") and links are NOT signals and return None.

Produces the same ParsedSignal type as the v1 parser so the executor/audit are shared.
"""
from __future__ import annotations

import re

from app.services.telegram_signals import ParsedSignal, ParsedTarget, _clean, _numbers

_SYMBOL_LINE_RE = re.compile(r"^[A-Za-z0-9]{2,15}$")
_TARGET_RE = re.compile(r"هدف\s*(\d+)\s*[:：]\s*(\d+(?:[.,]\d+)?)")
_STOP_RE = re.compile(r"(?:⛔️?\s*)?(?:ال)?وقف\s*[:：]\s*(\d+(?:[.,]\d+)?)")
_PRICE_LINE_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*\$?\s*$")
_MARKET_ENTRY_KEYS = ("دخول فوري", "دخول فورى")
_SECOND_ENTRY_KEYS = ("دخول ثاني", "دخول ثانى", "الدخول الثاني")
_NOT_SIGNAL_KEYS = ("متوسط الدخول", "سعر المستوى", "أُلغي الدخول", "الغي الدخول", "تم تفعيل الدخول")
MARKET_ENTRY_TOLERANCE = 0.015  # buy up to 1.5% above the posted "immediate entry" price, never chase further


def looks_like_signal_v2(text: str) -> bool:
    t = text or ""
    return any(k in t for k in _MARKET_ENTRY_KEYS) and "هدف" in t and "وقف" in t and not any(k in t for k in _NOT_SIGNAL_KEYS)


def parse_signal_v2(text: str) -> ParsedSignal | None:
    if not looks_like_signal_v2(text or ""):
        return None
    lines = [_clean(ln) for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return None

    # Symbol: first line that is a bare ticker (emojis stripped)
    base = None
    for ln in lines[:3]:
        candidate = re.sub(r"[^A-Za-z0-9]", "", ln)
        if candidate and _SYMBOL_LINE_RE.match(candidate) and not candidate.isdigit():
            base = candidate.upper()
            break
    if not base:
        return None

    entry = None
    second = None
    risk_note = None
    mode = None  # "entry" | "second"
    for ln in lines:
        low = ln.lower()
        if "خطورة" in ln:
            risk_note = ln.strip("⚠️🚨 ").strip()
            continue
        if any(k in ln for k in _MARKET_ENTRY_KEYS):
            mode = "entry"
            nums = _numbers(ln.split("فوري")[-1]) if "فوري" in ln else []
            if nums:
                entry = nums[0]
                mode = None
            continue
        if any(k in ln for k in _SECOND_ENTRY_KEYS):
            mode = "second"
            # "دخول ثاني (-5.0%)" -> the percentage is not the price; a price on the same line is rare
            tail = ln.split(")")[-1] if ")" in ln else ""
            nums = _numbers(tail)
            if nums:
                second = nums[0]
                mode = None
            continue
        m = _PRICE_LINE_RE.match(ln.replace("$", "").strip())
        if m and mode:
            val = float(m.group(1).replace(",", "."))
            if mode == "entry":
                entry = val
            else:
                second = val
            mode = None
            continue

    targets: list[ParsedTarget] = []
    for ln in lines:
        m = _TARGET_RE.search(ln)
        if not m or "دخول" in ln:
            continue
        price = float(m.group(2).replace(",", "."))
        pct = None
        pm = re.search(r"\(\s*\+?\s*(\d+(?:[.,]\d+)?)\s*%", ln)
        if pm:
            pct = float(pm.group(1).replace(",", "."))
        targets.append(ParsedTarget(price=price, pct=pct, sell_fraction=0.0))

    stop = None
    for ln in lines:
        m = _STOP_RE.search(ln)
        if m:
            stop = float(m.group(1).replace(",", "."))
            break

    if entry is None or not targets or stop is None:
        return None
    targets.sort(key=lambda t: t.price)
    n = len(targets)
    for t in targets:
        t.sell_fraction = 1.0 / n
    targets[-1].sell_fraction = max(0.0, 1.0 - sum(t.sell_fraction for t in targets[:-1]))

    warnings: list[str] = []
    if risk_note:
        warnings.append(risk_note)
    if second is not None and second >= entry:
        warnings.append("second entry not below the first; ignored")
        second = None
    sig = ParsedSignal(
        symbol=f"{base}USDT",
        base=base,
        exchange="binance",  # the channel does not name an exchange; listing is verified on the account at ingest
        entry_low=float(second) if second is not None else float(entry),
        entry_high=float(entry),
        stop_price=float(stop),
        stop_on_4h_close=False,
        targets=targets,
        warnings=warnings,
    )
    sig.entry_kind = "market"  # type: ignore[attr-defined]
    sig.leg2_price = float(second) if second is not None else None  # type: ignore[attr-defined]
    sig.risk_note = risk_note  # type: ignore[attr-defined]
    return sig
