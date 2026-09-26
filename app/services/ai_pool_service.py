"""
AI Trader Pool service — isolated sub-wallet trading inside a real account.

Hard isolation rules (never relaxed):
  1. The pool may only SPEND its own cash: allocated capital + realized profit.
  2. The pool may only SELL quantities it bought itself (per-position ledger),
     capped by the actual free balance. Other holdings are never touched.
  3. Every order carries an AIPOOL client-order id. The pool never cancels or
     reads other orders; exits are market orders when a rule triggers.
  4. Any external change to a pool-held asset is detected by reconciliation and
     accounted for conservatively (never assumed to be profit).

Two loops:
  * run_ai_pool_tick  — every ~10s: stops, take-profit, trailing, time-stop,
                        reconciliation, circuit breakers.
  * run_ai_pool_scan  — every ~5min: regime check + strategy scan + sized entries.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ai_pool import AiPool, AiPoolLog, AiPoolPosition, AiPoolTrade
from app.models.trading import AppSetting
from app.services.ai_strategy import Signal, btc_short_term_bias, market_regime, regime_min_score_adjust, scan_universe
from app.services.binance_public import get_prices

logger = logging.getLogger(__name__)

CLIENT_PREFIX = "AIPOOL"
MIN_NOTIONAL_FLOOR = 6.0  # USDT; Binance minimum is 5, keep a buffer for rounding
MIN_POOL_CAPITAL = 10.0
DUST_USDT = 0.5
RECONCILE_EVERY_SECONDS = 180
SUPPORTED_ACCOUNTS = {"binance_1": "Binance 1 (All Coins)"}
EXCLUDED_SYMBOLS_SETTING_KEY = "ai_pool_excluded_symbols"
WEAK_BTC_RISK_MULTIPLIER = 0.5
WEAK_BTC_MAX_GIVEBACK_PCT = 30.0  # when BTC turns weak intraday, protect open profit harder
DEFAULT_BREAKEVEN_AT_R = 0.6
DEFAULT_TP1_R = 1.2
DEFAULT_TP1_FRACTION = 0.4

RISK_PROFILES: dict[str, dict[str, float]] = {
    "conservative": dict(risk_per_trade_pct=1.0, max_position_pct=25.0, max_positions=3, min_entry_score=75.0, daily_loss_limit_pct=3.0, max_drawdown_pct=10.0, symbol_cooldown_hours=24.0, max_entries_per_hour=1, time_stop_hours=72.0, max_portfolio_risk_pct=2.5, breaker_cooldown_hours=24.0, breakeven_at_r=0.5, tp1_r=1.0, tp1_fraction=0.5, profit_giveback_pct=40.0),
    "balanced": dict(risk_per_trade_pct=1.5, max_position_pct=35.0, max_positions=4, min_entry_score=65.0, daily_loss_limit_pct=4.0, max_drawdown_pct=15.0, symbol_cooldown_hours=12.0, max_entries_per_hour=2, time_stop_hours=72.0, max_portfolio_risk_pct=4.0, breaker_cooldown_hours=12.0, breakeven_at_r=0.6, tp1_r=1.2, tp1_fraction=0.4, profit_giveback_pct=50.0),
    "aggressive": dict(risk_per_trade_pct=2.5, max_position_pct=45.0, max_positions=5, min_entry_score=55.0, daily_loss_limit_pct=6.0, max_drawdown_pct=20.0, symbol_cooldown_hours=6.0, max_entries_per_hour=3, time_stop_hours=96.0, max_portfolio_risk_pct=7.0, breaker_cooldown_hours=6.0, breakeven_at_r=1.0, tp1_r=1.5, tp1_fraction=0.4, profit_giveback_pct=60.0),
}

_LAST_SCAN: dict[int, dict[str, Any]] = {}
_LAST_RECONCILE_AT: dict[int, float] = {}
_SKIP_DEBOUNCE: dict[tuple[int, str], float] = {}
_tick_lock = threading.Lock()
_scan_lock = threading.Lock()
# One lock for every read-modify-write of a pool ledger (tick, scan entries, manual actions).
# Network-heavy work (market scans) happens OUTSIDE this lock so stop-loss ticks are never delayed.
_LEDGER_LOCK = threading.RLock()


# ── Exchange adapter ───────────────────────────────────────────────────────────

def _exchange(account: str):
    if account == "binance_1":
        from app.services import binance_live as ex
        return ex
    raise RuntimeError(f"AI pool: unsupported account '{account}' (only binance_1 for now)")


def _base_asset(symbol: str) -> str:
    s = symbol.upper()
    return s[:-4] if s.endswith("USDT") else s


def _client_id(pool_id: int, tag: str) -> str:
    return f"{CLIENT_PREFIX}-{pool_id}-{tag}-{int(time.time() * 1000) % 10_000_000_000}"[:36]


def _round_step_down(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step + 1e-9) * step


# ── Logging ────────────────────────────────────────────────────────────────────

def _log(db: Session, pool_id: Optional[int], event: str, message: str, symbol: Optional[str] = None) -> None:
    db.add(AiPoolLog(pool_id=pool_id, symbol=symbol, event=event, message=message))
    db.flush()
    logger.info("[AiPool %s] %s %s | %s", pool_id, event, symbol or "", message)


def _log_failure(db: Session, pool_id: int, event: str, exc: Exception) -> None:
    """Write a loop failure into the pool's own log (fresh transaction) so the UI shows it."""
    try:
        _log(db, pool_id, event, str(exc)[:400])
        db.commit()
    except Exception:
        db.rollback()


def _log_debounced(db: Session, pool_id: int, event: str, message: str, symbol: Optional[str] = None, every_seconds: int = 1800) -> None:
    key = (pool_id, f"{event}:{symbol or ''}")
    now = time.time()
    if _SKIP_DEBOUNCE.get(key, 0.0) > now:
        return
    _SKIP_DEBOUNCE[key] = now + every_seconds
    _log(db, pool_id, event, message, symbol)


# ── Pure helpers (unit-tested) ─────────────────────────────────────────────────

def finite_amount(value: Any, name: str = "amount") -> float:
    """float() accepts 'nan'/'inf'; those silently defeat every comparison-based guard. Reject them."""
    try:
        val = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(val):
        raise ValueError(f"{name} must be a finite number")
    return val


def compute_entry_notional(
    equity: float,
    cash: float,
    risk_per_trade_pct: float,
    max_position_pct: float,
    stop_distance_pct: float,
    min_notional: float,
) -> tuple[float, str]:
    """
    Volatility-based position size.

    notional = (equity * risk%) / stop_distance%  capped by max_position% of equity and by cash.
    If the sized notional is under the exchange minimum, the minimum is used only when the
    implied risk stays within 2x the configured risk; otherwise the trade is skipped.
    Returns (notional, reason). notional == 0 means skip.
    """
    if equity <= 0 or cash <= 0 or stop_distance_pct <= 0:
        return 0.0, "no_capital"
    risk_usdt = equity * risk_per_trade_pct / 100.0
    sized = risk_usdt / (stop_distance_pct / 100.0)
    cap = equity * max_position_pct / 100.0
    spendable = max(0.0, cash - 0.25)
    notional = min(sized, cap, spendable)
    if notional >= min_notional:
        return round(notional, 2), "sized"
    if spendable < min_notional:
        return 0.0, "cash_below_min_notional"
    implied_risk = min_notional * stop_distance_pct / 100.0
    if implied_risk <= risk_usdt * 2.0:
        return round(min_notional, 2), "min_notional"
    return 0.0, "min_notional_too_risky"


def trailing_stop_price(highest_price: float, atr_value: float, trail_mult: float) -> float:
    return highest_price - trail_mult * atr_value


def breakeven_price(avg_entry: float, fee_pct: float) -> float:
    """Entry plus round-trip fees so a breakeven exit does not lose money."""
    return avg_entry * (1.0 + 2.0 * fee_pct / 100.0 + 0.001)


def profit_ladder_stop(avg_entry: float, r: float, highest_price: float, fee_pct: float, breakeven_at_r: float = DEFAULT_BREAKEVEN_AT_R) -> float | None:
    """
    Minimum stop implied by how many R the trade has already reached (using the highest price):
    breakeven_at_r reached -> breakeven+fees; kR reached (k >= 2) -> lock (k-1)R. None when under the threshold.
    """
    if r <= 0 or avg_entry <= 0 or highest_price <= avg_entry:
        return None
    reached = (highest_price - avg_entry) / r
    if reached < max(0.1, breakeven_at_r):
        return None
    k = int(reached)
    if k < 2:
        return breakeven_price(avg_entry, fee_pct)
    return max(breakeven_price(avg_entry, fee_pct), avg_entry + (k - 1) * r)


# ── Pool lifecycle ─────────────────────────────────────────────────────────────

def _apply_profile(pool: AiPool, profile: str) -> None:
    p = RISK_PROFILES.get(profile) or RISK_PROFILES["balanced"]
    pool.risk_profile = profile if profile in RISK_PROFILES else "balanced"
    pool.risk_per_trade_pct = float(p["risk_per_trade_pct"])
    pool.max_position_pct = float(p["max_position_pct"])
    pool.max_positions = int(p["max_positions"])
    pool.min_entry_score = float(p["min_entry_score"])
    pool.daily_loss_limit_pct = float(p["daily_loss_limit_pct"])
    pool.max_drawdown_pct = float(p["max_drawdown_pct"])
    pool.symbol_cooldown_hours = float(p["symbol_cooldown_hours"])
    pool.max_entries_per_hour = int(p["max_entries_per_hour"])
    pool.time_stop_hours = float(p["time_stop_hours"])
    pool.max_portfolio_risk_pct = float(p.get("max_portfolio_risk_pct", 4.0))
    pool.breaker_cooldown_hours = float(p.get("breaker_cooldown_hours", 12.0))
    pool.breakeven_at_r = float(p.get("breakeven_at_r", DEFAULT_BREAKEVEN_AT_R))
    pool.tp1_r = float(p.get("tp1_r", DEFAULT_TP1_R))
    pool.tp1_fraction = float(p.get("tp1_fraction", DEFAULT_TP1_FRACTION))
    pool.profit_giveback_pct = float(p.get("profit_giveback_pct", 50.0))


def create_pool(db: Session, amount_usdt: float, risk_profile: str = "balanced", account: str = "binance_1", name: str = "AI Trader", kind: str = "ai", channel: Optional[str] = None) -> AiPool:
    amount = finite_amount(amount_usdt)
    if kind not in {"ai", "telegram"}:
        raise ValueError("Unsupported pool kind")
    if amount < MIN_POOL_CAPITAL:
        raise ValueError(f"Minimum pool capital is {MIN_POOL_CAPITAL:.0f} USDT")
    if account not in SUPPORTED_ACCOUNTS:
        raise ValueError("Unsupported account")
    ex = _exchange(account)
    if not ex.is_configured():
        raise RuntimeError("Exchange API keys are not configured for this account")
    with _LEDGER_LOCK:
        q = db.query(AiPool).filter(AiPool.account == account, AiPool.kind == kind, AiPool.status != "deleted")
        if kind == "telegram" and channel:
            q = q.filter(AiPool.channel == channel)
        existing = q.first()
        if existing:
            raise ValueError("A pool of this kind already exists on this account (for this channel). Add funds to it instead.")
        free = float(_balances_or_raise(ex).get("USDT", {}).get("free", 0.0))
        if free < amount:
            raise ValueError(f"Account free USDT ({free:.2f}) is below the requested amount ({amount:.2f})")
        pool = AiPool(name=name.strip() or ("Signals" if kind == "telegram" else "AI Trader"), account=account, status="running", kind=kind, channel=(channel or None) if kind == "telegram" else None)
        _apply_profile(pool, risk_profile)
        if kind == "telegram":
            pool.max_positions = 5  # slots: capital is split evenly across concurrent signals
            pool.last_target_sell_pct = 0.0  # the last slice runs with the coin behind a trailing stop
            pool.time_stop_hours = 720.0  # signals can take weeks; no time stop in practice
        pool.allocated_usdt = amount
        pool.cash_usdt = amount
        pool.peak_equity_usdt = amount
        pool.day_key = datetime.utcnow().strftime("%Y-%m-%d")
        pool.day_start_equity_usdt = amount
        db.add(pool)
        db.flush()
        _log(db, pool.id, "CREATE", f"Pool created with {amount:.2f} USDT on {SUPPORTED_ACCOUNTS[account]} | profile={pool.risk_profile}")
        db.commit()
    return pool


def add_funds(db: Session, pool: AiPool, amount_usdt: float) -> None:
    amount = finite_amount(amount_usdt)
    if amount <= 0:
        raise ValueError("Amount must be positive")
    ex = _exchange(pool.account)
    with _LEDGER_LOCK:
        db.refresh(pool)
        free = float(_balances_or_raise(ex).get("USDT", {}).get("free", 0.0))
        if free < float(pool.cash_usdt) + amount:
            raise ValueError(f"Account free USDT ({free:.2f}) cannot cover pool cash after deposit ({float(pool.cash_usdt) + amount:.2f})")
        pool.allocated_usdt = float(pool.allocated_usdt) + amount
        pool.cash_usdt = float(pool.cash_usdt) + amount
        _log(db, pool.id, "DEPOSIT", f"Added {amount:.2f} USDT | cash={pool.cash_usdt:.2f}")
        db.commit()


def withdraw_funds(db: Session, pool: AiPool, amount_usdt: float) -> None:
    amount = finite_amount(amount_usdt)
    if amount <= 0:
        raise ValueError("Amount must be positive")
    with _LEDGER_LOCK:
        db.refresh(pool)
        if amount > float(pool.cash_usdt):
            raise ValueError(f"Only {pool.cash_usdt:.2f} USDT cash is available to withdraw")
        pool.cash_usdt = float(pool.cash_usdt) - amount
        pool.allocated_usdt = max(0.0, float(pool.allocated_usdt) - amount)
        _log(db, pool.id, "WITHDRAW", f"Released {amount:.2f} USDT back to the account | cash={pool.cash_usdt:.2f}")
        db.commit()


def pause_pool(db: Session, pool: AiPool, reason: str = "manual") -> None:
    with _LEDGER_LOCK:
        db.refresh(pool)
        pool.status = "paused"
        pool.halt_reason = reason
        _log(db, pool.id, "PAUSE", f"Pool paused ({reason}). Exits keep running; no new entries.")
        db.commit()


def resume_pool(db: Session, pool: AiPool) -> None:
    with _LEDGER_LOCK:
        db.refresh(pool)
        reason = str(pool.halt_reason or "")
        pool.status = "running"
        pool.halt_reason = None
        pool.consecutive_errors = 0
        positions = _open_positions(db, pool)
        prices = _prices_for([p.symbol for p in positions])
        equity = pool_equity(pool, positions, prices)
        # Reset the breaker baselines so the breaker that fired does not re-fire on the next tick.
        pool.peak_equity_usdt = equity
        if reason.startswith("daily_loss"):
            pool.day_start_equity_usdt = equity
        _log(db, pool.id, "RESUME", f"Pool resumed by user (was: {reason or 'manual'}). Baselines reset at equity {equity:.2f}.")
        db.commit()


def update_settings(db: Session, pool: AiPool, **kwargs: Any) -> None:
    with _LEDGER_LOCK:
        db.refresh(pool)
        _update_settings_locked(db, pool, **kwargs)


def _update_settings_locked(db: Session, pool: AiPool, **kwargs: Any) -> None:
    allowed = {
        "risk_per_trade_pct": (0.25, 5.0),
        "max_position_pct": (10.0, 100.0),
        "max_positions": (1, 10),
        "min_entry_score": (40.0, 95.0),
        "daily_loss_limit_pct": (1.0, 20.0),
        "max_drawdown_pct": (3.0, 50.0),
        "symbol_cooldown_hours": (0.0, 168.0),
        "max_entries_per_hour": (1, 10),
        "time_stop_hours": (6.0, 720.0),
        "max_portfolio_risk_pct": (1.0, 20.0),
        "breaker_cooldown_hours": (0.0, 168.0),
        "profit_giveback_pct": (10.0, 100.0),
        "breakeven_at_r": (0.3, 1.5),
        "tp1_r": (0.8, 3.0),
        "tp1_fraction": (0.2, 0.8),
        "target_lock_pct": (0.0, 100.0),
        "runner_giveback_pct": (10.0, 100.0),
        "last_target_sell_pct": (0.0, 100.0),
        "entry_split_pct": (0.0, 100.0),
        "leg2_below_pct": (0.5, 15.0),
        "leg2_fallback_hours": (0.0, 168.0),
    }
    changed: list[str] = []
    for key, (lo, hi) in allowed.items():
        if key not in kwargs or kwargs[key] in (None, ""):
            continue
        val = finite_amount(kwargs[key], key)
        val = max(lo, min(hi, val))
        if key in {"max_positions", "max_entries_per_hour"}:
            val = int(val)
        setattr(pool, key, val)
        changed.append(f"{key}={val}")
    if kwargs.get("leg2_level") in {"bottom", "mid", "below_pct"}:
        pool.leg2_level = str(kwargs["leg2_level"])
        changed.append(f"leg2_level={pool.leg2_level}")
    if "avoid_account_holdings" in kwargs:
        pool.avoid_account_holdings = bool(kwargs["avoid_account_holdings"])
        changed.append(f"avoid_account_holdings={pool.avoid_account_holdings}")
    if "risk_profile" in kwargs and kwargs["risk_profile"] in RISK_PROFILES and kwargs["risk_profile"] != pool.risk_profile:
        _apply_profile(pool, str(kwargs["risk_profile"]))
        changed.append(f"profile={pool.risk_profile}")
    if changed:
        _log(db, pool.id, "SETTINGS", ", ".join(changed))
    db.commit()


# ── Queries ────────────────────────────────────────────────────────────────────

def _open_positions(db: Session, pool: AiPool) -> list[AiPoolPosition]:
    return db.query(AiPoolPosition).filter(AiPoolPosition.pool_id == pool.id, AiPoolPosition.status == "open").all()


def _prices_for(symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    try:
        return get_prices(sorted(set(symbols)))
    except Exception as exc:
        logger.warning("AI pool price fetch failed: %s", exc)
        return {}


def open_risk_usdt(positions: list[AiPoolPosition], prices: dict[str, float]) -> float:
    """Portfolio heat: what all open positions would lose if every stop were hit right now."""
    total = 0.0
    for p in positions:
        px = float(prices.get(p.symbol, p.current_price or p.avg_entry or 0.0))
        total += max(0.0, float(p.qty) * (px - float(p.stop_price or 0.0)))
    return total


def heat_capped_notional(notional: float, risk_pct: float, equity: float, max_portfolio_risk_pct: float, open_risk: float, min_notional: float) -> tuple[float, str]:
    """Shrink a sized entry so total open risk stays under the portfolio cap. 0 means skip."""
    if risk_pct <= 0 or equity <= 0:
        return 0.0, "invalid"
    cap = equity * max_portfolio_risk_pct / 100.0
    room = cap - open_risk
    if room <= 0:
        return 0.0, "heat_cap_full"
    new_risk = notional * risk_pct / 100.0
    if new_risk <= room:
        return notional, "ok"
    shrunk = room / (risk_pct / 100.0)
    if shrunk < min_notional:
        return 0.0, "heat_cap_room_below_min"
    return round(shrunk, 2), "heat_capped"


def excluded_symbols(db: Session) -> set[str]:
    row = db.query(AppSetting).filter(AppSetting.key == EXCLUDED_SYMBOLS_SETTING_KEY).first()
    if not row or not row.value:
        return set()
    return {s.strip().upper() for s in row.value.split(",") if s.strip()}


def _add_excluded_symbol(db: Session, symbol: str) -> None:
    current = excluded_symbols(db)
    current.add(symbol.upper())
    value = ",".join(sorted(current))[:120]
    row = db.query(AppSetting).filter(AppSetting.key == EXCLUDED_SYMBOLS_SETTING_KEY).first()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=EXCLUDED_SYMBOLS_SETTING_KEY, value=value))
    db.flush()


def _is_symbol_rejection(exc: Exception) -> bool:
    text = str(exc).lower()
    return ("-2010" in text and "not permitted" in text) or "-1121" in text or "invalid symbol" in text


def pool_equity(pool: AiPool, positions: list[AiPoolPosition], prices: dict[str, float]) -> float:
    value = float(pool.cash_usdt)
    for p in positions:
        px = float(prices.get(p.symbol, p.current_price or p.avg_entry or 0.0))
        value += float(p.qty) * px
    return value


# ── Order execution (ledger-safe) ──────────────────────────────────────────────

def _balances_or_raise(ex) -> dict[str, dict[str, float]]:
    """The exchange wrapper returns {} on API failure; the pool must never mistake that for 'empty'."""
    balances = ex.get_balances()
    if not balances:
        raise RuntimeError("balance fetch returned no data (API error or rate limit)")
    return balances


def _recover_order(ex, symbol: str, client_order_id: str) -> Optional[dict]:
    """
    After a request error we do not know whether Binance executed the order.
    Look it up by client id; return a fill summary if it exists and filled, else None.
    """
    lookup = getattr(ex, "get_order_by_client_id", None)
    if lookup is None:
        return None
    try:
        raw = lookup(symbol, client_order_id)
    except Exception as exc:
        logger.warning("AI pool: order lookup for %s failed: %s", client_order_id, exc)
        return None
    if not raw:
        return None
    executed = float(raw.get("executedQty", 0.0) or 0.0)
    quote_qty = float(raw.get("cummulativeQuoteQty", 0.0) or 0.0)
    if executed <= 0 or quote_qty <= 0:
        return None
    avg = quote_qty / executed
    fee_est = quote_qty * float(settings.trading_fee_pct) / 100.0
    is_buy = str(raw.get("side", "")).upper() == "BUY"
    fee_base = (fee_est / avg) if is_buy else 0.0
    return {
        "order_id": float(raw.get("orderId", 0) or 0),
        "status": str(raw.get("status", "")),
        "executed_qty": executed,
        "quote_qty": quote_qty,
        "avg_price": avg,
        "fee_base": fee_base,
        "fee_usdt": fee_est,
        "net_qty": max(0.0, executed - fee_base),
    }


def _commit_quietly(db: Session) -> None:
    try:
        db.commit()
    except Exception as exc:
        logger.error("AI pool: commit failed: %s", exc)
        db.rollback()


ERROR_STREAK_LIMIT = 5
ERROR_RATE_LIMIT = 8  # errors within ERROR_RATE_WINDOW_MIN, even if interleaved with successes
ERROR_RATE_WINDOW_MIN = 30


def _record_error(db: Session, pool: AiPool, symbol: str, message: str) -> None:
    pool.consecutive_errors = int(pool.consecutive_errors or 0) + 1
    _log(db, pool.id, "ERROR", message, symbol)
    if pool.status != "running":
        return
    if pool.consecutive_errors >= ERROR_STREAK_LIMIT:
        pool.status = "paused"
        pool.halt_reason = "api_errors"
        _log(db, pool.id, "BREAKER", f"{ERROR_STREAK_LIMIT} consecutive exchange errors — pool paused. Resume manually after checking the API.")
        return
    cutoff = datetime.utcnow() - timedelta(minutes=ERROR_RATE_WINDOW_MIN)
    recent = db.query(AiPoolLog).filter(AiPoolLog.pool_id == pool.id, AiPoolLog.event == "ERROR", AiPoolLog.created_at >= cutoff).count()
    if recent >= ERROR_RATE_LIMIT:
        pool.status = "paused"
        pool.halt_reason = "api_error_rate"
        _log(db, pool.id, "BREAKER", f"{recent} exchange errors in the last {ERROR_RATE_WINDOW_MIN} min — pool paused. Resume manually after checking the API.")


def _buy(db: Session, pool: AiPool, sig: Signal, quote_usdt: float, extra: Optional[dict] = None, merge_into: Optional[AiPoolPosition] = None) -> Optional[AiPoolPosition]:
    """
    Place one entry and COMMIT it immediately: a real fill must never be lost to a later rollback.
    `extra` fields (e.g. a telegram plan) are applied to the position BEFORE the same commit, so a
    position can never exist without its plan. With `merge_into`, the fill is added to that open
    position (second entry leg): qty, cost basis and average entry are merged.
    """
    try:
        return _buy_inner(db, pool, sig, quote_usdt, extra, merge_into)
    except Exception as exc:
        logger.exception("AI pool: unexpected error during BUY %s", sig.symbol)
        try:
            _log(db, pool.id, "ERROR", f"unexpected error during BUY: {exc}", sig.symbol)
        except Exception:
            pass
        return None
    finally:
        _commit_quietly(db)


def _buy_inner(db: Session, pool: AiPool, sig: Signal, quote_usdt: float, extra: Optional[dict] = None, merge_into: Optional[AiPoolPosition] = None) -> Optional[AiPoolPosition]:
    """
    Cash is RESERVED under the ledger lock, the exchange order is placed WITHOUT the lock (so a slow
    Binance response never delays stop-loss ticks of any pool), and the fill is recorded under the
    lock again. Callers must NOT hold _LEDGER_LOCK across this call.
    """
    ex = _exchange(pool.account)
    symbol = sig.symbol
    quote = round(float(quote_usdt), 2)
    try:
        free = float(_balances_or_raise(ex).get("USDT", {}).get("free", 0.0))
    except Exception as exc:
        with _LEDGER_LOCK:
            _record_error(db, pool, symbol, f"balance check failed: {exc}")
        return None

    # 1) reserve cash
    with _LEDGER_LOCK:
        db.refresh(pool)
        if quote > float(pool.cash_usdt):
            _log(db, pool.id, "SKIP_CASH", f"needs {quote:.2f} but pool cash is {pool.cash_usdt:.2f}", symbol)
            return None
        if free < quote:
            _log(db, pool.id, "SKIP_CASH", f"account free USDT {free:.2f} < {quote:.2f} (pool cash {pool.cash_usdt:.2f} is not fully available)", symbol)
            return None
        if pool.status != "running":
            _log(db, pool.id, "SKIP_PAUSED", f"pool is {pool.status}; entry skipped", symbol)
            return None
        pool.cash_usdt = float(pool.cash_usdt) - quote
        _commit_quietly(db)

    # 2) exchange call (no lock)
    cid = _client_id(pool.id, "E")
    res: Optional[dict] = None
    failure: Optional[str] = None
    rejected = False
    recovered_note: Optional[str] = None
    try:
        res = ex.place_market_buy_quote(symbol, quote, client_order_id=cid)
    except Exception as exc:
        if _is_symbol_rejection(exc):
            rejected = True
            failure = str(exc)
        else:
            res = _recover_order(ex, symbol, cid)
            if res is None:
                failure = f"BUY failed: {exc}"
            else:
                recovered_note = f"BUY request errored ({exc}) but order {cid} is {res['status']} on the exchange; recorded from lookup"

    # 3) record under the lock (release the reservation on failure)
    with _LEDGER_LOCK:
        db.refresh(pool)
        if res is None:
            pool.cash_usdt = float(pool.cash_usdt) + quote
            if rejected:
                _add_excluded_symbol(db, symbol)
                _log(db, pool.id, "EXCLUDE", f"exchange rejected this symbol for the account; permanently excluded from scans ({failure})", symbol)
            else:
                _record_error(db, pool, symbol, failure or "BUY failed")
                _LAST_RECONCILE_AT[pool.id] = 0.0  # verify holdings on the very next tick
            return None
        if recovered_note:
            _log(db, pool.id, "RECOVERED", recovered_note, symbol)
        executed = float(res.get("executed_qty", 0.0))
        spent = float(res.get("quote_qty", 0.0))
        avg = float(res.get("avg_price", 0.0))
        fee_usdt = float(res.get("fee_usdt", 0.0))
        fee_base = float(res.get("fee_base", 0.0))
        net_qty = float(res.get("net_qty", executed))
        if executed <= 0 or avg <= 0 or spent <= 0:
            pool.cash_usdt = float(pool.cash_usdt) + quote
            _record_error(db, pool, symbol, f"BUY returned empty fill: {res}")
            _LAST_RECONCILE_AT[pool.id] = 0.0
            return None
        pool.consecutive_errors = 0
        # reservation was `quote`; settle to what was actually spent
        pool.cash_usdt = float(pool.cash_usdt) + quote - spent
        cost_basis = spent
        if fee_base <= 0 and fee_usdt > 0:
            # Fee was charged in USDT/BNB rather than the bought asset: pay it from pool cash
            # and carry it in the cost basis so realized PnL accounts for it.
            pool.cash_usdt = float(pool.cash_usdt) - fee_usdt
            cost_basis += fee_usdt
        pool.fees_paid_usdt = float(pool.fees_paid_usdt) + fee_usdt

        if merge_into is not None and merge_into.status == "open":
            pos = merge_into
            old_qty = float(pos.qty)
            new_qty = old_qty + net_qty
            pos.avg_entry = ((float(pos.avg_entry) * old_qty) + avg * net_qty) / new_qty if new_qty > 0 else avg
            pos.qty = new_qty
            pos.qty_initial = float(pos.qty_initial) + net_qty
            pos.invested_usdt = float(pos.invested_usdt) + cost_basis
            pos.entry_fee_usdt = float(pos.entry_fee_usdt or 0.0) + fee_usdt
            pos.highest_price = max(float(pos.highest_price or 0.0), avg)
            pos.current_price = avg
            db.add(AiPoolTrade(pool_id=pool.id, position_id=pos.id, symbol=symbol, side="BUY", kind="add", qty=net_qty, price=avg, quote_usdt=spent, fee_usdt=fee_usdt, order_id=str(int(res.get("order_id", 0) or 0)), client_order_id=cid))
            _log(db, pool.id, "ENTRY_ADD", f"BUY {spent:.2f} USDT @ {avg:.6g} added ({net_qty:.8g} {_base_asset(symbol)}) | new avg {pos.avg_entry:.6g}, qty {pos.qty:.8g} | cash left {pool.cash_usdt:.2f}", symbol)
            return pos

        r = avg - sig.stop_price
        pos = AiPoolPosition(
            pool_id=pool.id,
            symbol=symbol,
            strategy=sig.strategy,
            status="open",
            qty=net_qty,
            qty_initial=net_qty,
            avg_entry=avg,
            invested_usdt=cost_basis,
            entry_fee_usdt=fee_usdt,
            stop_price=sig.stop_price,
            initial_stop_price=sig.stop_price,
            tp1_price=avg + max(0.0, r) * float(pool.tp1_r or DEFAULT_TP1_R),
            tp1_done=False,
            trail_atr=sig.atr_4h,
            trail_mult=sig.trail_mult,
            highest_price=avg,
            entry_score=sig.score,
            entry_reason=" | ".join(sig.reasons),
            current_price=avg,
        )
        for key, value in (extra or {}).items():
            setattr(pos, key, value)
        db.add(pos)
        db.flush()
        db.add(AiPoolTrade(pool_id=pool.id, position_id=pos.id, symbol=symbol, side="BUY", kind="entry", qty=net_qty, price=avg, quote_usdt=spent, fee_usdt=fee_usdt, order_id=str(int(res.get("order_id", 0) or 0)), client_order_id=cid))
        _log(
            db, pool.id, "ENTRY",
            f"BUY {spent:.2f} USDT @ {avg:.6g} ({net_qty:.8g} {_base_asset(symbol)}) | {sig.strategy} score {sig.score:.0f} | "
            f"stop {sig.stop_price:.6g} (-{sig.risk_pct:.1f}%) tp1 {pos.tp1_price:.6g} | cash left {pool.cash_usdt:.2f} | {pos.entry_reason}",
            symbol,
        )
        return pos


def _sell(db: Session, pool: AiPool, pos: AiPoolPosition, qty_wanted: float, kind: str, price_hint: float, balances: Optional[dict] = None) -> Optional[dict]:
    """Sell up to qty_wanted of the pool's OWN qty and COMMIT immediately. Never exceeds pos.qty nor free balance."""
    try:
        return _sell_inner(db, pool, pos, qty_wanted, kind, price_hint, balances)
    except Exception as exc:
        logger.exception("AI pool: unexpected error during SELL %s", pos.symbol)
        try:
            _log(db, pool.id, "ERROR", f"unexpected error during SELL ({kind}): {exc}", pos.symbol)
        except Exception:
            pass
        return None
    finally:
        _commit_quietly(db)


def _sell_inner(db: Session, pool: AiPool, pos: AiPoolPosition, qty_wanted: float, kind: str, price_hint: float, balances: Optional[dict] = None) -> Optional[dict]:
    plan = _plan_sell(db, pool, pos, qty_wanted, price_hint, balances)
    if plan is None:
        return None
    res, cid, err = _place_sell(_exchange(pool.account), pool.id, pos.symbol, plan["qty"])
    if res is None:
        _record_error(db, pool, pos.symbol, f"SELL failed ({kind}): {err}")
        _LAST_RECONCILE_AT[pool.id] = 0.0
        return None
    if err is not None:
        _log(db, pool.id, "RECOVERED", f"SELL request errored ({err}) but order {cid} is {res['status']} on the exchange; recorded from lookup", pos.symbol)
    return _record_sell(db, pool, pos, res, kind, cid, plan)


def _plan_sell(db: Session, pool: AiPool, pos: AiPoolPosition, qty_wanted: float, price_hint: float, balances: Optional[dict] = None) -> Optional[dict]:
    """Decide the exact sellable qty (never above pool qty or free balance). Returns None when nothing can be sold."""
    ex = _exchange(pool.account)
    symbol = pos.symbol
    base = _base_asset(symbol)
    try:
        if not balances:
            balances = _balances_or_raise(ex)
        filters = ex.get_symbol_lot_filters(symbol)
    except Exception as exc:
        _record_error(db, pool, symbol, f"pre-sell lookup failed: {exc}")
        return None
    free = float(balances.get(base, {}).get("free", 0.0))
    step = float(filters.get("step_size", 0.0) or 0.0)
    min_qty = float(filters.get("min_qty", 0.0) or 0.0)
    min_notional = max(float(filters.get("min_notional", 0.0) or 0.0), 5.0)
    px = float(price_hint or pos.current_price or pos.avg_entry or 0.0)

    owned = float(pos.qty)
    qty = min(float(qty_wanted), owned, free)
    qty = _round_step_down(qty, step)
    remaining_value = (owned - qty) * px
    if remaining_value < min_notional * 1.1:
        qty = _round_step_down(min(owned, free), step)  # remainder would be unsellable dust: sell everything
    if free <= 0 or qty <= 0 or qty < min_qty or qty * px < min_notional:
        if owned * px < max(DUST_USDT, min_notional * 0.2) or free <= 0:
            _close_position_record(db, pool, pos, px, "dust" if free > 0 else "external", note=f"unsellable remainder {owned:.8g} {base} (free {free:.8g})")
            return None
        _log(db, pool.id, "SKIP_SELL", f"cannot sell {qty:.8g} {base}: below lot/notional limits (free {free:.8g})", symbol)
        return None
    return {"qty": qty, "min_qty": min_qty, "base": base, "owned": owned}


def _place_sell(ex, pool_id: int, symbol: str, qty: float) -> tuple[Optional[dict], str, Optional[Exception]]:
    """Exchange call only (thread-safe, no DB). Returns (fill, client_id, error). error set with a fill means recovered."""
    cid = _client_id(pool_id, "X")
    try:
        return ex.place_market_sell_qty(symbol, qty, client_order_id=cid), cid, None
    except Exception as exc:
        recovered = _recover_order(ex, symbol, cid)
        return recovered, cid, exc


def _record_sell(db: Session, pool: AiPool, pos: AiPoolPosition, res: dict, kind: str, cid: str, plan: dict) -> Optional[dict]:
    symbol = pos.symbol
    base = plan["base"]
    owned = float(pos.qty)
    min_qty = float(plan["min_qty"])
    executed = float(res.get("executed_qty", 0.0))
    received = float(res.get("quote_qty", 0.0))
    avg = float(res.get("avg_price", 0.0))
    fee_usdt = float(res.get("fee_usdt", 0.0))
    if executed <= 0 or received <= 0:
        _record_error(db, pool, symbol, f"SELL returned empty fill: {res}")
        return None
    pool.consecutive_errors = 0
    executed = min(executed, owned)
    proceeds = received - fee_usdt
    # Proportional cost basis (invested_usdt already includes the entry fee).
    cost = float(pos.invested_usdt) * (executed / owned) if owned > 0 else 0.0
    pnl = proceeds - cost

    pool.cash_usdt = float(pool.cash_usdt) + proceeds
    pool.realized_pnl_usdt = float(pool.realized_pnl_usdt) + pnl
    pool.fees_paid_usdt = float(pool.fees_paid_usdt) + fee_usdt
    pos.qty = max(0.0, owned - executed)
    pos.invested_usdt = max(0.0, float(pos.invested_usdt) - cost)
    pos.realized_pnl_usdt = float(pos.realized_pnl_usdt or 0.0) + pnl
    db.add(AiPoolTrade(pool_id=pool.id, position_id=pos.id, symbol=symbol, side="SELL", kind=kind, qty=executed, price=avg, quote_usdt=received, fee_usdt=fee_usdt, pnl_usdt=pnl, order_id=str(int(res.get("order_id", 0) or 0)), client_order_id=cid))
    _log(
        db, pool.id, "EXIT_" + kind.upper(),
        f"SELL {executed:.8g} {base} @ {avg:.6g} = {received:.2f} USDT | pnl {pnl:+.2f} | remaining {pos.qty:.8g} | cash {pool.cash_usdt:.2f}",
        symbol,
    )
    if pos.qty * avg < DUST_USDT or pos.qty < min_qty:
        _close_position_record(db, pool, pos, avg, kind)
    return {"executed": executed, "avg": avg, "pnl": pnl}


def _close_position_record(db: Session, pool: AiPool, pos: AiPoolPosition, price: float, reason: str, note: str = "") -> None:
    remaining_cost = float(pos.invested_usdt or 0.0)
    if remaining_cost > 0:
        # Whatever is left (unsellable dust, or an asset removed externally) is written off
        # conservatively so pool equity never counts value the pool cannot realize.
        pool.realized_pnl_usdt = float(pool.realized_pnl_usdt) - remaining_cost
        pos.realized_pnl_usdt = float(pos.realized_pnl_usdt or 0.0) - remaining_cost
        pos.invested_usdt = 0.0
        if reason not in {"external"}:
            note = (note + f" | wrote off {remaining_cost:.4f} USDT of unsellable remainder").strip(" |")
    pos.qty = 0.0
    pos.status = "closed"
    pos.closed_at = datetime.utcnow()
    pos.close_reason = reason
    pos.current_price = price
    pos.unrealized_pnl_usdt = 0.0
    pos.unrealized_pnl_pct = 0.0
    total = float(pos.realized_pnl_usdt or 0.0)
    if total >= 0:
        pool.trades_won = int(pool.trades_won or 0) + 1
    else:
        pool.trades_lost = int(pool.trades_lost or 0) + 1
    _log(db, pool.id, "CLOSED", f"position closed ({reason}) total pnl {total:+.2f} USDT {note}".strip(), pos.symbol)
    if getattr(pos, "signal_id", None):
        from app.services.telegram_signal_service import sync_signal_status  # lazy: avoids import cycle

        sync_signal_status(db, pos)


# ── Tick: exits, reconciliation, breakers ──────────────────────────────────────

def _reconcile(db: Session, pool: AiPool, positions: list[AiPoolPosition], prices: dict[str, float]) -> None:
    ex = _exchange(pool.account)
    try:
        balances = ex.get_balances()
    except Exception as exc:
        logger.warning("AI pool reconcile skipped: %s", exc)
        return
    if not balances:
        return
    for pos in positions:
        base = _base_asset(pos.symbol)
        b = balances.get(base, {})
        actual = float(b.get("free", 0.0)) + float(b.get("locked", 0.0))
        owned = float(pos.qty)
        if owned <= 0:
            continue
        if actual < owned * 0.98:
            px = float(prices.get(pos.symbol, pos.current_price or pos.avg_entry))
            pos.external_flag = f"external reduction {owned:.8g} -> {actual:.8g} at {datetime.utcnow():%H:%M}"
            _log(db, pool.id, "EXTERNAL", f"pool owned {owned:.8g} {base} but account holds {actual:.8g}; ledger adjusted down", pos.symbol)
            if actual * px < DUST_USDT:
                pos.qty = 0.0
                _close_position_record(db, pool, pos, px, "external")
            else:
                pos.invested_usdt = float(pos.invested_usdt) * (actual / owned)
                pos.qty = actual


def _apply_breakers(db: Session, pool: AiPool, equity: float) -> None:
    if not math.isfinite(equity):
        if pool.status != "halted":
            pool.status = "halted"
            pool.halt_reason = "invalid_equity"
            _log(db, pool.id, "BREAKER", f"Equity is not a finite number ({equity}); pool HALTED for manual inspection.")
        return
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if pool.day_key != today:
        pool.day_key = today
        pool.day_start_equity_usdt = equity
    if pool.status == "paused" and (pool.halt_reason or "").startswith("daily_loss"):
        # Lift only when BOTH a new UTC day started and the cooldown since the breaker has elapsed.
        fired_at = pool.breaker_at or datetime.utcnow()
        cooled = (datetime.utcnow() - fired_at) >= timedelta(hours=float(pool.breaker_cooldown_hours or 0.0))
        new_day = fired_at.strftime("%Y-%m-%d") != today
        if cooled and new_day:
            pool.status = "running"
            pool.halt_reason = None
            pool.breaker_at = None
            pool.day_start_equity_usdt = equity
            _log(db, pool.id, "RESUME", f"Daily-loss pause lifted after {pool.breaker_cooldown_hours:.0f}h cooldown and a new UTC day. Baseline {equity:.2f}.")
    if equity > float(pool.peak_equity_usdt or 0.0):
        pool.peak_equity_usdt = equity
    if pool.status != "running":
        return
    day_start = float(pool.day_start_equity_usdt or 0.0)
    if day_start > 0:
        day_loss_pct = (day_start - equity) / day_start * 100.0
        if day_loss_pct >= float(pool.daily_loss_limit_pct):
            pool.status = "paused"
            pool.halt_reason = f"daily_loss {day_loss_pct:.1f}%"
            pool.breaker_at = datetime.utcnow()
            _log(db, pool.id, "BREAKER", f"Daily loss {day_loss_pct:.1f}% >= {pool.daily_loss_limit_pct:.1f}% — no new entries for at least {pool.breaker_cooldown_hours:.0f}h and until a new UTC day.")
            return
    peak = float(pool.peak_equity_usdt or 0.0)
    if peak > 0:
        dd = (peak - equity) / peak * 100.0
        if dd >= float(pool.max_drawdown_pct):
            pool.status = "halted"
            pool.halt_reason = f"drawdown {dd:.1f}%"
            _log(db, pool.id, "BREAKER", f"Drawdown {dd:.1f}% from peak >= {pool.max_drawdown_pct:.1f}% — pool HALTED. Exits still protected; resume manually.")


def _manage_position(db: Session, pool: AiPool, pos: AiPoolPosition, price: float, regime: str, balances: Optional[dict] = None, defer_full_exits: bool = False) -> Optional[str]:
    """
    Update live fields and apply exit rules. With defer_full_exits=True, a full exit (stop/trail/time_stop)
    is NOT executed here; the kind is returned so the caller can fire several stops in parallel.
    """
    if price <= 0 or float(pos.qty) <= 0:
        return None
    if (pos.strategy or "") == "telegram":
        from app.services.telegram_signal_service import manage_signal_position  # lazy: avoids import cycle
        return manage_signal_position(db, pool, pos, price, balances, defer_full_exits)
    pos.current_price = price
    pos.highest_price = max(float(pos.highest_price or 0.0), price)
    invested = float(pos.invested_usdt)
    value = float(pos.qty) * price
    pos.unrealized_pnl_usdt = value - invested
    pos.unrealized_pnl_pct = (price / float(pos.avg_entry) - 1.0) * 100.0 if float(pos.avg_entry) > 0 else 0.0

    r = float(pos.avg_entry) - float(pos.initial_stop_price)
    trail_mult = float(pos.trail_mult or 2.5)
    if regime == "strong_bearish":
        trail_mult = min(trail_mult, 1.5)
        if price > breakeven_price(float(pos.avg_entry), settings.trading_fee_pct):
            pos.stop_price = max(float(pos.stop_price), breakeven_price(float(pos.avg_entry), settings.trading_fee_pct))

    # 1) Hard stop / trailing stop
    if price <= float(pos.stop_price):
        kind = "trail" if pos.tp1_done or float(pos.stop_price) > float(pos.initial_stop_price) else "stop"
        if defer_full_exits:
            return kind
        _sell(db, pool, pos, float(pos.qty), kind, price, balances)
        return None

    # 2) Partial take-profit at TP1, then move stop to breakeven
    if not pos.tp1_done and float(pos.tp1_price) > 0 and price >= float(pos.tp1_price):
        fraction = float(pool.tp1_fraction or DEFAULT_TP1_FRACTION)
        res = _sell(db, pool, pos, float(pos.qty) * fraction, "tp1", price, balances)
        if res is not None or pos.status == "closed":
            pos.tp1_done = True
            if pos.status == "open":
                pos.stop_price = max(float(pos.stop_price), breakeven_price(float(pos.avg_entry), settings.trading_fee_pct))
                _log(db, pool.id, "STOP_MOVE", f"TP1 hit: stop moved to breakeven {pos.stop_price:.6g}", pos.symbol)
        if pos.status != "open":
            return None
        # fall through: the ladder/trail below only moves the stop, it never sells again in this tick

    # 3) Profit-protection ladder + trailing stop. The stop only ever moves up.
    #    Ladder (based on the HIGHEST price reached, so it ratchets):
    #      >= 1R profit -> stop at breakeven + fees (a winner must never turn into a loser)
    #      >= 2R profit -> stop locks +1R
    #      >= 3R profit -> stop locks +2R, and so on
    #    Trailing (ATR chandelier) runs alongside; the higher of the two wins.
    be_r = float(pool.breakeven_at_r or DEFAULT_BREAKEVEN_AT_R)
    floor = profit_ladder_stop(float(pos.avg_entry), r, float(pos.highest_price), settings.trading_fee_pct, be_r)
    if floor is not None and floor > float(pos.stop_price):
        old_stop = float(pos.stop_price)
        pos.stop_price = floor
        _log(db, pool.id, "STOP_MOVE", f"profit ladder: stop {old_stop:.6g} -> {floor:.6g} (reached {(float(pos.highest_price) - float(pos.avg_entry)) / r:.1f}R)", pos.symbol)
    # Give-back guard: once past 1R, never hand back more than profit_giveback_pct of the peak open profit.
    # (50% default: a trade that reached +8% cannot close below +4%.) Set to 100 to disable.
    giveback = float(pool.profit_giveback_pct if pool.profit_giveback_pct is not None else 50.0)
    if btc_short_term_bias() == "weak":
        giveback = min(giveback, WEAK_BTC_MAX_GIVEBACK_PCT)  # sudden BTC weakness: hold profit tighter
    peak_gain = float(pos.highest_price) - float(pos.avg_entry)
    if r > 0 and peak_gain >= be_r * r and giveback < 100.0:
        guard = float(pos.avg_entry) + peak_gain * (1.0 - giveback / 100.0)
        if guard > float(pos.stop_price):
            pos.stop_price = guard
    activated = pos.tp1_done or (r > 0 and float(pos.highest_price) >= float(pos.avg_entry) + r)
    if activated and float(pos.trail_atr) > 0:
        new_stop = trailing_stop_price(float(pos.highest_price), float(pos.trail_atr), trail_mult)
        if new_stop > float(pos.stop_price):
            pos.stop_price = new_stop

    # 4) Time stop: no progress after N hours
    age_h = (datetime.utcnow() - (pos.opened_at or datetime.utcnow())).total_seconds() / 3600.0
    if age_h >= float(pool.time_stop_hours) and float(pos.unrealized_pnl_pct) < 0.5 and not pos.tp1_done:
        _log(db, pool.id, "TIME_STOP", f"{age_h:.0f}h without progress ({pos.unrealized_pnl_pct:+.2f}%) — exiting", pos.symbol)
        if defer_full_exits:
            return "time_stop"
        _sell(db, pool, pos, float(pos.qty), "time_stop", price, balances)
    return None


def _execute_exits(db: Session, pool: AiPool, exits: list[tuple[AiPoolPosition, str, float]], balances: Optional[dict]) -> None:
    """
    Fire several full exits at once: plan each (DB, sequential), place all orders in parallel
    (exchange only), then record fills sequentially. In a flash crash every second of latency costs.
    """
    if not exits:
        return
    ex = _exchange(pool.account)
    if balances is None:
        try:
            balances = _balances_or_raise(ex)  # one snapshot for all exits of this tick
        except Exception as exc:
            logger.warning("AI pool: balance snapshot failed, sells will fetch individually: %s", exc)
    if len(exits) == 1:
        pos, kind, px = exits[0]
        _sell(db, pool, pos, float(pos.qty), kind, px, balances)
        return
    plans: list[tuple[AiPoolPosition, str, dict]] = []
    for pos, kind, px in exits:
        plan = _plan_sell(db, pool, pos, float(pos.qty), px, balances)
        if plan is not None:
            plans.append((pos, kind, plan))
    _commit_quietly(db)
    if not plans:
        return
    # Plain values only cross the thread boundary: ORM instances are expired after commit and must
    # never be touched from worker threads.
    pool_id = int(pool.id)
    jobs = [(str(pos.symbol), float(plan["qty"])) for pos, _kind, plan in plans]
    with ThreadPoolExecutor(max_workers=min(4, len(jobs)), thread_name_prefix="ai-exit") as tp:
        results = list(tp.map(lambda job: _place_sell(ex, pool_id, job[0], job[1]), jobs))
    for (pos, kind, plan), (res, cid, err) in zip(plans, results):
        try:
            if res is None:
                _record_error(db, pool, pos.symbol, f"SELL failed ({kind}): {err}")
                _LAST_RECONCILE_AT[pool.id] = 0.0
            else:
                if err is not None:
                    _log(db, pool.id, "RECOVERED", f"SELL request errored ({err}) but order {cid} is {res['status']} on the exchange; recorded from lookup", pos.symbol)
                _record_sell(db, pool, pos, res, kind, cid, plan)
        except Exception as exc:
            logger.exception("AI pool: recording parallel exit for %s failed", pos.symbol)
            _log(db, pool.id, "ERROR", f"recording exit failed: {exc}", pos.symbol)
        finally:
            _commit_quietly(db)


def run_ai_pool_tick(db: Session) -> None:
    if not _tick_lock.acquire(blocking=False):
        return
    try:
        if not db.query(AiPool.id).filter(AiPool.status.in_(["running", "paused", "halted"])).first():
            return
        regime = market_regime()
        with _LEDGER_LOCK:
            _tick_locked(db, regime)
    finally:
        _tick_lock.release()


def _tick_locked(db: Session, regime: str) -> None:
    db.expire_all()
    pools = db.query(AiPool).filter(AiPool.status.in_(["running", "paused", "halted"])).all()
    for pool in pools:
            try:
                positions = _open_positions(db, pool)
                prices = _prices_for([p.symbol for p in positions])
                if positions and not prices:
                    _log_debounced(db, pool.id, "PRICE_FEED_DOWN", "no prices available; stop/TP checks skipped this tick", every_seconds=300)
                now = time.time()
                if positions and now - _LAST_RECONCILE_AT.get(pool.id, 0.0) >= RECONCILE_EVERY_SECONDS:
                    _LAST_RECONCILE_AT[pool.id] = now
                    _reconcile(db, pool, positions, prices)
                    positions = [p for p in positions if p.status == "open"]
                # Balances are fetched lazily (only when an exit is about to be placed): the account
                # endpoint costs weight 20 and a 5s tick across two accounts was feeding Binance IP bans.
                balances: Optional[dict] = None
                exits: list[tuple[AiPoolPosition, str, float]] = []
                for pos in positions:
                    px = float(prices.get(pos.symbol, 0.0))
                    if px > 0:
                        kind = _manage_position(db, pool, pos, px, regime, balances, defer_full_exits=True)
                        if kind:
                            exits.append((pos, kind, px))
                _execute_exits(db, pool, exits, balances)
                positions = [p for p in positions if p.status == "open"]
                equity = pool_equity(pool, positions, prices)
                _apply_breakers(db, pool, equity)
                pool.market_state = regime
                pool.last_tick_at = datetime.utcnow()
                db.commit()
            except Exception as exc:
                db.rollback()
                logger.error("AI pool tick error (pool %s): %s", pool.id, exc)
                _log_failure(db, pool.id, "TICK_ERROR", exc)


# ── Scan: entries ──────────────────────────────────────────────────────────────

def _account_holdings_symbols(ex, open_symbols: set[str]) -> set[str]:
    """Symbols the account already holds outside the pool (value >= 1 USDT)."""
    try:
        balances = ex.get_balances()
    except Exception:
        return set()
    candidates = []
    for asset, b in balances.items():
        if asset in {"USDT", "BNB", "USDC", "FDUSD", "BUSD", "TUSD"}:
            continue
        total = float(b.get("free", 0.0)) + float(b.get("locked", 0.0))
        if total <= 0:
            continue
        sym = f"{asset}USDT"
        if sym in open_symbols:
            continue
        candidates.append((sym, total))
    if not candidates:
        return set()
    prices = _prices_for([c[0] for c in candidates])
    return {sym for sym, total in candidates if total * float(prices.get(sym, 0.0)) >= 1.0}


def _recently_traded_symbols(db: Session, pool: AiPool) -> set[str]:
    out: set[str] = set()
    cutoff_stop = datetime.utcnow() - timedelta(hours=float(pool.symbol_cooldown_hours))
    cutoff_any = datetime.utcnow() - timedelta(hours=2)
    rows = db.query(AiPoolTrade).filter(AiPoolTrade.pool_id == pool.id, AiPoolTrade.side == "SELL", AiPoolTrade.created_at >= min(cutoff_stop, cutoff_any)).all()
    for t in rows:
        if t.kind in {"stop", "time_stop"} and t.created_at >= cutoff_stop:
            out.add(t.symbol)
        elif t.created_at >= cutoff_any:
            out.add(t.symbol)
    return out


def _entries_last_hour(db: Session, pool: AiPool) -> int:
    cutoff = datetime.utcnow() - timedelta(hours=1)
    return db.query(AiPoolTrade).filter(AiPoolTrade.pool_id == pool.id, AiPoolTrade.side == "BUY", AiPoolTrade.created_at >= cutoff).count()


def run_ai_pool_scan(db: Session) -> None:
    if not _scan_lock.acquire(blocking=False):
        return
    try:
        pools = db.query(AiPool).filter(AiPool.status == "running").all()
        if not pools:
            return
        regime = market_regime()
        for pool in pools:
            try:
                if (pool.kind or "ai") == "telegram":
                    from app.services.telegram_signal_service import check_pending_signals  # lazy: avoids import cycle
                    check_pending_signals(db, pool)
                    pool.last_scan_at = datetime.utcnow()
                    db.commit()
                    continue
                _scan_pool(db, pool, regime)
                pool.last_scan_at = datetime.utcnow()
                pool.market_state = regime
                db.commit()
            except Exception as exc:
                db.rollback()
                logger.error("AI pool scan error (pool %s): %s", pool.id, exc)
                _log_failure(db, pool.id, "SCAN_ERROR", exc)
    finally:
        _scan_lock.release()


def _scan_pool(db: Session, pool: AiPool, regime: str) -> None:
    ex = _exchange(pool.account)
    positions = _open_positions(db, pool)
    open_symbols = {p.symbol for p in positions}
    state: dict[str, Any] = {"at": datetime.utcnow().isoformat(timespec="seconds"), "regime": regime, "signals": [], "scanned": [], "note": ""}
    _LAST_SCAN[pool.id] = state

    if regime == "strong_bearish":
        state["note"] = "BTC strong bearish regime: no new entries."
        _log_debounced(db, pool.id, "SKIP_REGIME", "BTC regime strong_bearish — entries disabled")
        return
    if int(pool.max_positions) - len(positions) <= 0:
        state["note"] = "All position slots are in use."
        return
    if float(pool.cash_usdt) < MIN_NOTIONAL_FLOOR:
        state["note"] = f"Pool cash {pool.cash_usdt:.2f} USDT is below the minimum order size."
        _log_debounced(db, pool.id, "SKIP_CASH", f"cash {pool.cash_usdt:.2f} < min order {MIN_NOTIONAL_FLOOR:.0f} USDT")
        return
    if int(pool.max_entries_per_hour) - _entries_last_hour(db, pool) <= 0:
        state["note"] = "Hourly entry limit reached."
        return

    exclude = set(open_symbols) | _recently_traded_symbols(db, pool) | excluded_symbols(db)
    if pool.avoid_account_holdings:
        exclude |= _account_holdings_symbols(ex, open_symbols)

    # Network-heavy scan runs WITHOUT the ledger lock so stop-loss ticks are never blocked.
    signals, scanned = scan_universe(regime, exclude=exclude)
    state["scanned"] = scanned
    state["signals"] = [s.to_dict() for s in signals[:15]]
    min_score = float(pool.min_entry_score) + regime_min_score_adjust(regime)
    qualified = [s for s in signals if s.score >= min_score]
    if not qualified:
        state["note"] = f"{len(scanned)} symbols scanned, {len(signals)} setups found, none reached the entry score {min_score:.0f}."
        return

    _enter_signals_locked(db, pool, ex, qualified, state)


def _enter_signals_locked(db: Session, pool: AiPool, ex, qualified: list[Signal], state: dict[str, Any]) -> None:
    """
    Size and place entries. Sizing is re-computed under the ledger lock for every signal; the
    exchange order itself (_buy) runs without the lock so stop checks are never delayed.
    """
    bias = btc_short_term_bias()
    state["btc_bias"] = bias
    opened = 0
    for sig in qualified:
        with _LEDGER_LOCK:
            db.refresh(pool)
            if pool.status != "running":
                state["note"] = f"Pool is {pool.status}; entries skipped."
                return
            positions = _open_positions(db, pool)
            open_symbols = {p.symbol for p in positions}
            prices = _prices_for(list(open_symbols))
            equity = pool_equity(pool, positions, prices)
            slots = int(pool.max_positions) - len(positions)
            entries_left = int(pool.max_entries_per_hour) - _entries_last_hour(db, pool)
            if slots <= 0 or entries_left <= 0:
                state["note"] = "Slots or hourly entry limit exhausted."
                return
            if sig.symbol in open_symbols:
                continue
            risk_pct = float(pool.risk_per_trade_pct)
            if bias == "weak":
                risk_pct *= WEAK_BTC_RISK_MULTIPLIER
                _log_debounced(db, pool.id, "BTC_WEAK", f"BTC below 1h EMA20 or down >1% in 4h: entry risk halved to {risk_pct:.2f}%", every_seconds=1800)
            open_risk = open_risk_usdt(positions, prices)
            max_heat = float(pool.max_portfolio_risk_pct or 4.0)
            state["open_risk_usdt"] = round(open_risk, 2)
            min_notional = MIN_NOTIONAL_FLOOR
            try:
                f = ex.get_symbol_lot_filters(sig.symbol)
                min_notional = max(MIN_NOTIONAL_FLOOR, float(f.get("min_notional", 0.0) or 0.0) * 1.15)
            except Exception as exc:
                logger.warning("lot filter lookup failed for %s: %s", sig.symbol, exc)
            notional, why = compute_entry_notional(equity, float(pool.cash_usdt), risk_pct, float(pool.max_position_pct), sig.risk_pct, min_notional)
            if notional <= 0:
                _log_debounced(db, pool.id, "SKIP_SIZE", f"{why} (equity {equity:.2f}, cash {pool.cash_usdt:.2f}, stop {sig.risk_pct:.1f}%)", sig.symbol, every_seconds=900)
                _commit_quietly(db)
                continue
            notional, heat_why = heat_capped_notional(notional, sig.risk_pct, equity, max_heat, open_risk, min_notional)
            if notional <= 0:
                _log_debounced(db, pool.id, "SKIP_HEAT", f"{heat_why}: open risk {open_risk:.2f} of cap {equity * max_heat / 100.0:.2f} USDT ({max_heat:.1f}% of equity)", sig.symbol, every_seconds=900)
                state["note"] = f"portfolio risk cap reached ({open_risk:.2f} USDT open risk)."
                _commit_quietly(db)
                return
            _commit_quietly(db)
        pos = _buy(db, pool, sig, notional)  # outside the lock: reserves cash itself
        if pos is not None:
            opened += 1
    state["note"] = f"{len(qualified)} qualified, {opened} entered (BTC {bias}, open risk {state.get('open_risk_usdt', 0.0)} USDT)."


# ── Manual actions ─────────────────────────────────────────────────────────────

def manual_sell_position(db: Session, position_id: int, fraction: float = 1.0) -> dict:
    frac = finite_amount(fraction, "fraction")
    if frac <= 0.0 or frac > 1.0:
        raise ValueError("fraction must be between 0 and 1")
    frac = max(0.05, frac)
    with _LEDGER_LOCK:
        db.expire_all()
        pos = db.query(AiPoolPosition).filter(AiPoolPosition.id == position_id, AiPoolPosition.status == "open").first()
        if not pos:
            return {"ok": False, "error": "position not found or already closed"}
        pool = db.query(AiPool).filter(AiPool.id == pos.pool_id).first()
        if not pool:
            return {"ok": False, "error": "pool not found"}
        prices = _prices_for([pos.symbol])
        px = float(prices.get(pos.symbol, pos.current_price or pos.avg_entry))
        res = _sell(db, pool, pos, float(pos.qty) * frac, "manual", px)
        db.commit()
    if res is None:
        return {"ok": False, "error": "sell not executed (see log)"}
    return {"ok": True, **res}


def close_all_positions(db: Session, pool: AiPool) -> int:
    with _LEDGER_LOCK:
        db.refresh(pool)
        positions = _open_positions(db, pool)
        prices = _prices_for([p.symbol for p in positions])
        n = 0
        for pos in positions:
            px = float(prices.get(pos.symbol, pos.current_price or pos.avg_entry))
            if _sell(db, pool, pos, float(pos.qty), "close_all", px) is not None:
                n += 1
        _log(db, pool.id, "CLOSE_ALL", f"user requested close-all: {n}/{len(positions)} sold")
        db.commit()
    return n


# ── Read models for the UI ─────────────────────────────────────────────────────

def _targets_for(p: AiPoolPosition, price: float) -> dict:
    """Human-readable targets: R as %, TP1 %, ladder levels (price and %), and how many R reached."""
    entry = float(p.avg_entry or 0.0)
    r = entry - float(p.initial_stop_price or 0.0)
    if entry <= 0 or r <= 0:
        return {"r_pct": 0.0, "tp1_pct": 0.0, "ladder": [], "reached_r": 0.0}
    r_pct = r / entry * 100.0
    highest = max(float(p.highest_price or 0.0), price)
    ladder = []
    be_r = float(p.pool.breakeven_at_r or DEFAULT_BREAKEVEN_AT_R) if p.pool is not None else DEFAULT_BREAKEVEN_AT_R
    for k in (1, 2, 3, 4):
        trigger = entry + (be_r if k == 1 else k) * r
        lock = breakeven_price(entry, settings.trading_fee_pct) if k == 1 else entry + (k - 1) * r
        ladder.append({
            "k": k,
            "trigger_price": trigger,
            "trigger_pct": k * r_pct,
            "lock_price": lock,
            "lock_pct": (lock / entry - 1.0) * 100.0,
            "reached": highest >= trigger,
        })
    return {
        "r_pct": r_pct,
        "tp1_pct": (float(p.tp1_price or 0.0) / entry - 1.0) * 100.0 if p.tp1_price else 0.0,
        "ladder": ladder,
        "reached_r": (highest - entry) / r,
    }


def capture_stats(p: AiPoolPosition) -> dict:
    """
    How much of the best paper profit a trade actually banked.
      peak_pct     = highest price reached vs entry
      max_r        = that peak in R multiples
      capture_pct  = realized pnl / peak open profit (100 = banked everything the trade ever showed)
    Used after ~30 closed trades to tune the give-back guard from data instead of feel.
    """
    entry = float(p.avg_entry or 0.0)
    qty0 = float(p.qty_initial or 0.0)
    highest = float(p.highest_price or 0.0)
    r = entry - float(p.initial_stop_price or 0.0)
    peak_profit = qty0 * max(0.0, highest - entry)
    realized = float(p.realized_pnl_usdt or 0.0)
    return {
        "peak_pct": (highest / entry - 1.0) * 100.0 if entry > 0 else 0.0,
        "max_r": (highest - entry) / r if r > 0 else 0.0,
        "peak_profit_usdt": peak_profit,
        "capture_pct": (realized / peak_profit * 100.0) if peak_profit > 0.05 else None,
    }


def position_to_dict(p: AiPoolPosition) -> dict:
    return {
        "targets": _targets_for(p, float(p.current_price or p.avg_entry or 0.0)),
        "capture": capture_stats(p),
        "id": p.id,
        "symbol": p.symbol,
        "strategy": p.strategy,
        "status": p.status,
        "qty": float(p.qty or 0.0),
        "avg_entry": float(p.avg_entry or 0.0),
        "invested_usdt": float(p.invested_usdt or 0.0),
        "current_price": float(p.current_price or 0.0),
        "stop_price": float(p.stop_price or 0.0),
        "initial_stop_price": float(p.initial_stop_price or 0.0),
        "tp1_price": float(p.tp1_price or 0.0),
        "tp1_done": bool(p.tp1_done),
        "unrealized_pnl_usdt": float(p.unrealized_pnl_usdt or 0.0),
        "unrealized_pnl_pct": float(p.unrealized_pnl_pct or 0.0),
        "realized_pnl_usdt": float(p.realized_pnl_usdt or 0.0),
        "entry_score": float(p.entry_score or 0.0),
        "entry_reason": p.entry_reason or "",
        "external_flag": p.external_flag,
        "opened_at": p.opened_at.strftime("%Y-%m-%d %H:%M") if p.opened_at else "",
        "closed_at": p.closed_at.strftime("%Y-%m-%d %H:%M") if p.closed_at else "",
        "close_reason": p.close_reason,
    }


def pool_summary(db: Session, pool: AiPool, refresh_prices: bool = True) -> dict:
    positions = _open_positions(db, pool)
    prices = _prices_for([p.symbol for p in positions]) if refresh_prices else {}
    invested = 0.0
    market = 0.0
    rows = []
    for p in positions:
        px = float(prices.get(p.symbol, p.current_price or p.avg_entry or 0.0))
        d = position_to_dict(p)
        if px > 0:
            d["current_price"] = px
            d["unrealized_pnl_usdt"] = float(p.qty) * px - float(p.invested_usdt)
            d["unrealized_pnl_pct"] = (px / float(p.avg_entry) - 1.0) * 100.0 if float(p.avg_entry) > 0 else 0.0
            d["targets"] = _targets_for(p, px)
        invested += float(p.invested_usdt)
        market += float(p.qty) * px
        rows.append(d)
    equity = float(pool.cash_usdt) + market
    allocated = float(pool.allocated_usdt)
    peak = float(pool.peak_equity_usdt or equity)
    won = int(pool.trades_won or 0)
    lost = int(pool.trades_lost or 0)
    scan = _LAST_SCAN.get(pool.id, {})
    closed_rows = db.query(AiPoolPosition).filter(AiPoolPosition.pool_id == pool.id, AiPoolPosition.status == "closed").all()
    captures = [c["capture_pct"] for c in (capture_stats(p) for p in closed_rows) if c["capture_pct"] is not None]
    big_winners = sum(1 for p in closed_rows if capture_stats(p)["max_r"] >= 3.0)
    return {
        "id": pool.id,
        "capture_avg_pct": (sum(captures) / len(captures)) if captures else None,
        "capture_samples": len(captures),
        "big_winners_3r": big_winners,
        "closed_total": len(closed_rows),
        "name": pool.name,
        "account": pool.account,
        "account_label": SUPPORTED_ACCOUNTS.get(pool.account, pool.account),
        "status": pool.status,
        "halt_reason": pool.halt_reason,
        "risk_profile": pool.risk_profile,
        "market_state": pool.market_state or scan.get("regime") or "-",
        "allocated_usdt": allocated,
        "cash_usdt": float(pool.cash_usdt),
        "invested_usdt": invested,
        "market_value_usdt": market,
        "equity_usdt": equity,
        "unrealized_pnl_usdt": market - invested,
        "realized_pnl_usdt": float(pool.realized_pnl_usdt),
        "fees_paid_usdt": float(pool.fees_paid_usdt),
        "total_pnl_usdt": equity - allocated,
        "total_pnl_pct": ((equity / allocated - 1.0) * 100.0) if allocated > 0 else 0.0,
        "drawdown_pct": ((peak - equity) / peak * 100.0) if peak > 0 else 0.0,
        "peak_equity_usdt": peak,
        "trades_won": won,
        "trades_lost": lost,
        "win_rate_pct": (won / (won + lost) * 100.0) if (won + lost) > 0 else 0.0,
        "open_count": len(rows),
        "positions": rows,
        "settings": {
            "risk_per_trade_pct": float(pool.risk_per_trade_pct),
            "max_position_pct": float(pool.max_position_pct),
            "max_positions": int(pool.max_positions),
            "min_entry_score": float(pool.min_entry_score),
            "daily_loss_limit_pct": float(pool.daily_loss_limit_pct),
            "max_drawdown_pct": float(pool.max_drawdown_pct),
            "symbol_cooldown_hours": float(pool.symbol_cooldown_hours),
            "max_entries_per_hour": int(pool.max_entries_per_hour),
            "time_stop_hours": float(pool.time_stop_hours),
            "avoid_account_holdings": bool(pool.avoid_account_holdings),
            "max_portfolio_risk_pct": float(pool.max_portfolio_risk_pct or 4.0),
            "breaker_cooldown_hours": float(pool.breaker_cooldown_hours or 12.0),
            "profit_giveback_pct": float(pool.profit_giveback_pct if pool.profit_giveback_pct is not None else 50.0),
            "breakeven_at_r": float(pool.breakeven_at_r or DEFAULT_BREAKEVEN_AT_R),
            "tp1_r": float(pool.tp1_r or DEFAULT_TP1_R),
            "tp1_fraction": float(pool.tp1_fraction or DEFAULT_TP1_FRACTION),
            "target_lock_pct": float(pool.target_lock_pct if pool.target_lock_pct is not None else 50.0),
            "runner_giveback_pct": float(pool.runner_giveback_pct if pool.runner_giveback_pct is not None else 30.0),
            "last_target_sell_pct": float(pool.last_target_sell_pct if pool.last_target_sell_pct is not None else 50.0),
            "entry_split_pct": float(pool.entry_split_pct if pool.entry_split_pct is not None else 50.0),
            "leg2_level": str(pool.leg2_level or "mid"),
            "leg2_below_pct": float(pool.leg2_below_pct if pool.leg2_below_pct is not None else 3.0),
            "leg2_fallback_hours": float(pool.leg2_fallback_hours if pool.leg2_fallback_hours is not None else 24.0),
        },
        "open_risk_usdt": open_risk_usdt(positions, prices),
        "last_scan": scan,
        "last_scan_at": pool.last_scan_at.strftime("%Y-%m-%d %H:%M:%S") if pool.last_scan_at else None,
        "last_tick_at": pool.last_tick_at.strftime("%Y-%m-%d %H:%M:%S") if pool.last_tick_at else None,
    }


def recent_logs(db: Session, pool_id: int, limit: int = 80) -> list[dict]:
    rows = db.query(AiPoolLog).filter(AiPoolLog.pool_id == pool_id).order_by(desc(AiPoolLog.id)).limit(limit).all()
    return [{"id": r.id, "time": r.created_at.strftime("%m-%d %H:%M:%S") if r.created_at else "", "event": r.event, "symbol": r.symbol or "", "message": r.message} for r in rows]


def recent_trades(db: Session, pool_id: int, limit: int = 60) -> list[dict]:
    rows = db.query(AiPoolTrade).filter(AiPoolTrade.pool_id == pool_id).order_by(desc(AiPoolTrade.id)).limit(limit).all()
    return [
        {
            "id": t.id,
            "time": t.created_at.strftime("%m-%d %H:%M:%S") if t.created_at else "",
            "symbol": t.symbol,
            "side": t.side,
            "kind": t.kind,
            "qty": float(t.qty or 0.0),
            "price": float(t.price or 0.0),
            "quote_usdt": float(t.quote_usdt or 0.0),
            "fee_usdt": float(t.fee_usdt or 0.0),
            "pnl_usdt": float(t.pnl_usdt) if t.pnl_usdt is not None else None,
        }
        for t in rows
    ]


def closed_positions(db: Session, pool_id: int, limit: int = 50) -> list[dict]:
    rows = db.query(AiPoolPosition).filter(AiPoolPosition.pool_id == pool_id, AiPoolPosition.status == "closed").order_by(desc(AiPoolPosition.closed_at)).limit(limit).all()
    return [position_to_dict(p) for p in rows]
