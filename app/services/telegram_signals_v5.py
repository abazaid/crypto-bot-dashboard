"""
Tolerant fallback parser: tried after v1..v4, for layouts the channels improvise (no colons, no "#",
"XXXUSDT" symbols, "Tp1 0.34" targets, no stop at all). Shaban vip, 2026-10-04:

    Api3usdt spot
    Okx
    Entry 0.2930
    Tp1 0.3430
    Tp2 0.4267
    Tp3 0.5061

Rules:
  * symbol: "#API3", "API3/USDT", "API3-USDT" or "API3USDT" (any case);
  * entry: a line with Entry / Buy zone / Buy / منطقة الدخول / الدخول / السعر, one price (market entry
    at that price, never chased beyond the market tolerance) or two (a zone); the next line is used
    when the label stands alone;
  * targets: "TP1 x", "T1: x", "Target 1 - x", "هدف 1 x", "1️⃣ x", or numbered / bare price lines
    under a "Targets" header;
  * stop: SL / Stop / Stop loss / وقف / الوقف / ستوب / 🛑. When the post has none, the bot sets one
    TELEGRAM_DEFAULT_STOP_PCT below the entry (a trade is never opened without a stop).
The exchange named in the post (OKX...) is ignored: listing on the account is verified at ingest.
Futures, closed and progress posts ("TP1 hit ✅", "Entered entry zone") are not signals.
"""
from __future__ import annotations

import re

from app.core.config import settings
from app.services.telegram_signals import ParsedSignal, ParsedTarget, _clean, _numbers
from app.services.telegram_signals_v3 import is_futures

_N = r"\$?\s*(\d+(?:[.,]\d+)?)"
_HASH_RE = re.compile(r"#\s*([A-Za-z0-9]{1,15})\b")
_PAIR_RE = re.compile(r"\b([A-Za-z0-9]{1,15}?)\s*[/\-_ ]?\s*USDT\b", re.IGNORECASE)
_ENTRY_RE = re.compile(r"(?:\bentry(?:\s*zone)?\b|\bbuy(?:\s*zone)?\b|منطقة الدخول|الدخول|السعر)\s*[:=\-–]?\s*(.*)", re.IGNORECASE)
_STOP_RE = re.compile(r"(?:\bstop(?:\s*loss)?\b|\bsl\b|وقف(?:\s*الخسارة)?|الوقف|ستوب|🛑)\s*[:=\-–]?\s*(.*)", re.IGNORECASE)
_TARGET_RE = re.compile(r"(?:\btp|\bt|\btarget|هدف|الهدف)\s*(\d{1,2})\s*[:=\-–→)]?\s*" + _N, re.IGNORECASE)
_KEYCAP_RE = re.compile(r"^(\d{1,2})️?⃣\s*" + _N)
_NUMBERED_RE = re.compile(r"^\(?(\d{1,2})\s*(?:\)|[.\-:]\s)\s*" + _N)  # "1) 0.55", "2- 0.60"; never "0.55"
_BARE_PRICE_RE = re.compile(r"^" + _N + r"\s*$")
_TARGETS_HEADER_RE = re.compile(r"\btargets?\b|\btps?\b|الأهداف|الاهداف|أهداف|اهداف", re.IGNORECASE)
_NOT_SIGNAL_RE = re.compile(
    r"🏁\s*Closed|\bclosed\b|تم الإغلاق|الحالة:\s*مغلقة|تحقق|تم تحقيق|\bhit\b|\breached\b|\bachieved\b|\bentered\b",
    re.IGNORECASE,
)
_HIT_MARK_RE = re.compile(r"\d[^\n]*✅")  # "1️⃣ 2.20✅" = target already hit (a lone "✅ note" is decoration)
_NOT_A_COIN = {"USDT", "USD", "SPOT", "OKX", "BINANCE"}


def _symbol(lines: list[str]) -> str | None:
    body = "\n".join(lines)
    m = _HASH_RE.search(body)
    if m and m.group(1).upper() not in _NOT_A_COIN:
        return m.group(1).upper()
    for ln in lines:
        for m in _PAIR_RE.finditer(ln):
            base = m.group(1).upper()
            if base and base not in _NOT_A_COIN and not base.isdigit():
                return base
    return None


def _after_label(lines: list[str], rx: re.Pattern) -> str | None:
    for i, ln in enumerate(lines):
        m = rx.search(ln)
        if not m:
            continue
        rest = m.group(1).strip()
        if not re.search(r"\d", rest) and i + 1 < len(lines):
            rest = lines[i + 1]
        if re.search(r"\d", rest):
            return rest
    return None


def _targets(lines: list[str]) -> list[float]:
    by_index: dict[int, float] = {}
    bare: list[float] = []
    in_targets = False
    for ln in lines:
        hit = False
        for m in _TARGET_RE.finditer(ln):
            by_index.setdefault(int(m.group(1)), float(m.group(2).replace(",", ".")))
            hit = True
        if hit:
            continue
        km = _KEYCAP_RE.match(ln) or (_NUMBERED_RE.match(ln) if in_targets else None)
        if km:
            by_index.setdefault(int(km.group(1)), float(km.group(2).replace(",", ".")))
            continue
        if _ENTRY_RE.search(ln) or _STOP_RE.search(ln):
            in_targets = False
            continue
        if _TARGETS_HEADER_RE.search(ln) and not re.search(r"\d", ln):
            in_targets = True
            continue
        if in_targets:
            bm = _BARE_PRICE_RE.match(ln)
            if bm:
                bare.append(float(bm.group(1).replace(",", ".")))
            else:
                in_targets = False
    prices = list(by_index.values()) or bare
    return sorted(prices)


def looks_like_signal_v5(text: str) -> bool:
    """Loose on purpose: a post that names a coin, an entry and targets is recorded even when it cannot be parsed."""
    t = text or ""
    if _NOT_SIGNAL_RE.search(t) or _HIT_MARK_RE.search(t) or is_futures(t):
        return False
    lines = [ln for ln in (_clean(x) for x in t.splitlines()) if ln]
    has_targets = bool(_TARGET_RE.search(t)) or any(_KEYCAP_RE.match(ln) for ln in lines) or bool(_TARGETS_HEADER_RE.search(t))
    return bool(_symbol(lines) and _ENTRY_RE.search(t) and has_targets)


def parse_signal_v5(text: str) -> ParsedSignal | None:
    if not looks_like_signal_v5(text or ""):
        return None
    lines = [ln for ln in (_clean(x) for x in (text or "").splitlines()) if ln]
    base = _symbol(lines)
    entry_text = _after_label(lines, _ENTRY_RE)
    if not (base and entry_text):
        return None
    zone = _numbers(entry_text)[:2]
    if not zone:
        return None
    entry_low, entry_high = min(zone), max(zone)
    prices = [p for p in _targets(lines) if p > entry_high]
    if not prices:
        return None

    warnings: list[str] = []
    stop_text = _after_label(lines, _STOP_RE)
    stop = None
    if stop_text:
        nums = _numbers(stop_text.split("أسفل", 1)[1] if "أسفل" in stop_text else stop_text)
        stop = nums[0] if nums else None
    if stop is None:
        pct = float(settings.telegram_default_stop_pct)
        stop = round(entry_low * (1.0 - pct / 100.0), 10)
        warnings.append(f"no stop in the post; bot stop set {pct:g}% below the entry")

    targets = [ParsedTarget(price=p, pct=None, sell_fraction=1.0 / len(prices)) for p in prices]
    targets[-1].sell_fraction = max(0.0, 1.0 - sum(t.sell_fraction for t in targets[:-1]))
    sig = ParsedSignal(
        symbol=f"{base}USDT",
        base=base,
        exchange="binance",  # channels name OKX etc.; we trade Binance spot, listing verified at ingest
        entry_low=entry_low,
        entry_high=entry_high,
        stop_price=float(stop),
        stop_on_4h_close=False,
        targets=targets,
        warnings=warnings,
    )
    if len(zone) == 1:
        sig.entry_kind = "market"
    return sig
