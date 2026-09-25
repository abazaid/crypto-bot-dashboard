"""
Parser for "Shaban Signals"-style Telegram posts.

Expected shape (Arabic labels, LTR numbers; token order inside a line may vary):

    🚀 SHAABAN ELITE SIGNAL
    💎 #ALICE | Binance
    📍 الدخول: 0.1480 – 0.1556
    🎯 الأهداف:
    1️⃣ 0.171 | +9.9% | بيع 20%
    2️⃣ 0.187 | +20.2% | بيع 25%
    3️⃣ 0.210 | +35.0% | بيع 25%
    4️⃣ 0.233 | +49.7% | بيع 30%
    🛑 الستوب: إغلاق 4 ساعات أسفل 0.137
    ⚠️ تقسيم الدخول وإدارة رأس المال.

Pure functions only: no network, no DB. Unit-tested with real samples.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_NUM = r"\d+(?:[.,]\d+)?"
_NUM_RE = re.compile(_NUM)
_SYMBOL_RE = re.compile(r"#\s*([0-9A-Za-z]{2,15})")
_PCT_RE = re.compile(r"\+?\s*(" + _NUM + r")\s*%")
_SELL_RE = re.compile(r"(?:بيع|sell)\s*:?\s*(" + _NUM + r")\s*%", re.IGNORECASE)
_TARGET_LINE_RE = re.compile(r"^\s*(?:[1-9]️?⃣|\(?\d\)?[\.\-:)]?)\s*")
_ENTRY_KEYS = ("الدخول", "دخول", "entry", "buy zone", "buy")
_STOP_KEYS = ("الستوب", "ستوب", "وقف", "stop", "sl")
_TARGET_KEYS = ("الأهداف", "الاهداف", "هدف", "target", "tp")
_CLOSE_4H_KEYS = ("إغلاق 4", "اغلاق 4", "4h close", "4h")


@dataclass
class ParsedTarget:
    price: float
    pct: float | None
    sell_fraction: float  # 0..1


@dataclass
class ParsedSignal:
    symbol: str  # e.g. ALICEUSDT
    base: str
    exchange: str | None
    entry_low: float
    entry_high: float
    stop_price: float
    stop_on_4h_close: bool
    targets: list[ParsedTarget] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_binance(self) -> bool:
        return (self.exchange or "").lower() == "binance"

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "base": self.base,
            "exchange": self.exchange,
            "entry_low": self.entry_low,
            "entry_high": self.entry_high,
            "stop_price": self.stop_price,
            "stop_on_4h_close": self.stop_on_4h_close,
            "targets": [{"price": t.price, "pct": t.pct, "sell_fraction": t.sell_fraction} for t in self.targets],
            "warnings": list(self.warnings),
        }


_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")  # 1,234 / 67,234.50 -> 1234 / 67234.50


def _numbers(text: str) -> list[float]:
    text = _THOUSANDS_RE.sub("", text)
    out = []
    for m in _NUM_RE.finditer(text):
        try:
            out.append(float(m.group(0).replace(",", ".")))
        except ValueError:
            continue
    return out


def _clean(line: str) -> str:
    # strip zero-width / RTL control marks that Telegram inserts around Arabic text
    return re.sub(r"[​-‏‪-‮⁦-⁩]", "", line).strip()


def looks_like_signal(text: str) -> bool:
    t = text or ""
    return "#" in t and any(k in t for k in _ENTRY_KEYS) and any(k in t for k in _TARGET_KEYS)


def parse_signal(text: str) -> ParsedSignal | None:
    """Return a ParsedSignal, or None when the text is not a recognizable signal."""
    if not text or not looks_like_signal(text):
        return None
    lines = [_clean(ln) for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]

    symbol_m = _SYMBOL_RE.search(text)
    if not symbol_m:
        return None
    base = symbol_m.group(1).upper()
    exchange = None
    for ln in lines:
        if "#" + base.lower() in ln.lower() or f"#{base}" in ln:
            low = ln.lower()
            for ex in ("binance", "bybit", "okx", "kucoin", "mexc", "gate", "bitget"):
                if ex in low:
                    exchange = ex
                    break
            break

    entry_low = entry_high = None
    stop_price = None
    stop_on_4h = False
    targets: list[ParsedTarget] = []
    warnings: list[str] = []
    in_targets = False

    for ln in lines:
        low = ln.lower()
        if any(k in low for k in _ENTRY_KEYS) and entry_low is None:
            # only the part after the label (a "(zone 1)" style note must not become a price)
            nums = _numbers(ln.split(":", 1)[-1] if ":" in ln else ln)
            if len(nums) >= 2:
                entry_low, entry_high = min(nums[:2]), max(nums[:2])
            elif len(nums) == 1:
                entry_low = entry_high = nums[0]
            in_targets = False
            continue
        if any(k in low for k in _STOP_KEYS) and stop_price is None and "بيع" not in ln:
            nums = _numbers(ln)
            stop_on_4h = any(k in low for k in _CLOSE_4H_KEYS)
            # "إغلاق 4 ساعات أسفل 0.137" -> the 4 is a duration, the level is the last number
            level_nums = [n for n in nums if not (stop_on_4h and n == 4.0)]
            if level_nums:
                stop_price = level_nums[-1]
            in_targets = False
            continue
        if any(k in low for k in _TARGET_KEYS) and not _SELL_RE.search(ln) and "%" not in ln:
            in_targets = True
            continue
        sell_m = _SELL_RE.search(ln)
        pct_m = _PCT_RE.search(ln)
        if sell_m or (in_targets and pct_m):
            # price = first number that is not a percentage and not the leading index
            body = _TARGET_LINE_RE.sub("", ln)
            body_wo_pct = _PCT_RE.sub(" ", body)
            body_wo_sell = _SELL_RE.sub(" ", body_wo_pct)
            nums = _numbers(body_wo_sell)
            if not nums:
                continue
            price = nums[0]
            pct = None
            if pct_m:
                # the gain % is the "+x%" token; the sell % is handled separately
                for m in _PCT_RE.finditer(body):
                    span = m.span()
                    if sell_m and sell_m.span()[0] <= span[0] <= sell_m.span()[1]:
                        continue
                    pct = float(m.group(1).replace(",", "."))
                    break
            frac = float(sell_m.group(1).replace(",", ".")) / 100.0 if sell_m else 0.0
            targets.append(ParsedTarget(price=price, pct=pct, sell_fraction=frac))

    if entry_low is None or entry_high is None or not targets:
        return None
    if stop_price is None:
        warnings.append("no stop in signal")
        stop_price = 0.0

    # Normalise sell fractions: missing -> equal split; total != 100% -> scale; last takes remainder.
    total = sum(t.sell_fraction for t in targets)
    if total <= 0:
        for t in targets:
            t.sell_fraction = 1.0 / len(targets)
    elif abs(total - 1.0) > 0.01:
        warnings.append(f"sell fractions sum to {total * 100:.0f}%, rescaled")
        for t in targets:
            t.sell_fraction = t.sell_fraction / total
    targets[-1].sell_fraction = max(0.0, 1.0 - sum(t.sell_fraction for t in targets[:-1]))

    sig = ParsedSignal(
        symbol=f"{base}USDT",
        base=base,
        exchange=exchange,
        entry_low=float(entry_low),
        entry_high=float(entry_high),
        stop_price=float(stop_price),
        stop_on_4h_close=stop_on_4h,
        targets=targets,
        warnings=warnings,
    )
    return sig


def validate_signal(sig: ParsedSignal) -> list[str]:
    """Return a list of hard problems (empty list = executable)."""
    problems: list[str] = []
    if not sig.is_binance:
        problems.append(f"exchange is {sig.exchange or 'unknown'}, not Binance")
    if sig.entry_low <= 0 or sig.entry_high <= 0:
        problems.append("entry zone missing")
    if sig.stop_price <= 0:
        problems.append("stop missing")
    elif sig.stop_price >= sig.entry_low:
        problems.append("stop is not below the entry zone")
    prev = sig.entry_high
    for i, t in enumerate(sig.targets, start=1):
        if t.price <= prev:
            problems.append(f"target {i} ({t.price}) is not above the previous level")
        prev = max(prev, t.price)
    if sig.entry_high > 0 and sig.stop_price > 0:
        risk_pct = (sig.entry_high - sig.stop_price) / sig.entry_high * 100.0
        if risk_pct > 25.0:
            problems.append(f"stop {risk_pct:.0f}% away is too wide")
    return problems
