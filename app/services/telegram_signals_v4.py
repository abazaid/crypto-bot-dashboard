"""
Parser for single-price "Entry / SL / TP1..TP4" posts (Shaban Signals, Mar–Jul 2026 layouts):

    🚀 SHAABAN ELITE SIGNAL
    💎 #0G | 15 m
    🏦 Available on: Binance | KuCoin | MEXC
    💰 Entry: $0.181
    🛑 SL: $0.163
    🎯 Targets
    • TP1: $0.19 (+4.97%)
    ...
    • TP4: $0.27 (+49.17%)

Also "⬜ TP1 → $0.4846 (+5.00%)" target lines and the Arabic variant ("💰 السعر: $0.00340",
"🛑 الوقف: $0.00306"). There is no zone: the entry is a market entry at the posted price (never chased
beyond the market tolerance). Targets carry no sell percentages, so each gets an equal share.
Posts edited to "🏁 Closed" / "تم الإغلاق" and target-hit updates are not signals.
"""
from __future__ import annotations

import re

from app.services.telegram_signals import ParsedSignal, ParsedTarget, _clean

_NUM = r"\$?\s*(\d+(?:[.,]\d+)?)"
_SYMBOL_RE = re.compile(r"#([A-Za-z0-9]{1,15})\b")  # single-letter coins exist: #T, #F, #A
_ENTRY_RE = re.compile(r"(?:\bEntry|السعر)\s*:\s*" + _NUM, re.IGNORECASE)
_STOP_RE = re.compile(r"(?:\bSL|الوقف)\s*:\s*" + _NUM, re.IGNORECASE)
_TARGET_RE = re.compile(r"\bTP\s*(\d+)\s*(?::|→)\s*" + _NUM, re.IGNORECASE)
_AVAILABLE_RE = re.compile(r"Available on\s*:\s*(.+)", re.IGNORECASE)
_NOT_SIGNAL_RE = re.compile(r"🏁\s*Closed|تم الإغلاق|الحالة:\s*مغلقة|تحقق الهدف|تم تحقيق")


def looks_like_signal_v4(text: str) -> bool:
    t = text or ""
    return bool(_ENTRY_RE.search(t) and _STOP_RE.search(t) and _TARGET_RE.search(t)) and not _NOT_SIGNAL_RE.search(t)


def parse_signal_v4(text: str) -> ParsedSignal | None:
    if not looks_like_signal_v4(text or ""):
        return None
    body = "\n".join(ln for ln in (_clean(x) for x in (text or "").splitlines()) if ln)
    sm = _SYMBOL_RE.search(body)
    em = _ENTRY_RE.search(body)
    stm = _STOP_RE.search(body)
    if not (sm and em and stm):
        return None
    entry = float(em.group(1).replace(",", "."))
    stop = float(stm.group(1).replace(",", "."))

    by_index: dict[int, float] = {}
    for m in _TARGET_RE.finditer(body):
        by_index.setdefault(int(m.group(1)), float(m.group(2).replace(",", ".")))
    if not by_index:
        return None
    targets = [ParsedTarget(price=p, pct=None, sell_fraction=0.0) for p in sorted(by_index.values())]
    n = len(targets)
    for t in targets:
        t.sell_fraction = 1.0 / n
    targets[-1].sell_fraction = max(0.0, 1.0 - sum(t.sell_fraction for t in targets[:-1]))

    # "Available on: KuCoin | MEXC" (no Binance) -> not tradable here; no such line -> Binance assumed,
    # listing is verified on the account at ingest anyway.
    am = _AVAILABLE_RE.search(body)
    exchange = "binance" if (am is None or "binance" in am.group(1).lower()) else am.group(1).split("|")[0].strip().lower()

    base = sm.group(1).upper()
    sig = ParsedSignal(
        symbol=f"{base}USDT",
        base=base,
        exchange=exchange,
        entry_low=entry,
        entry_high=entry,
        stop_price=stop,
        stop_on_4h_close=False,
        targets=targets,
    )
    sig.entry_kind = "market"
    return sig
