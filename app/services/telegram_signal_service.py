"""
Telegram signal follower — executes channel signals inside an isolated pool.

Rules (agreed with the user):
  * Only NEW posts after activation are traded (dedupe by channel + message id; edits ignored).
  * Enter at market when the price is inside the entry zone; if above it, wait up to
    TELEGRAM_ENTRY_WINDOW_HOURS for it to come back, else mark "missed". Never chase.
  * Size = pool equity / slots (pool.max_positions), capped by cash; skip when cash < min order.
  * Exits: sell the channel's fraction at each target; after target 1 the stop goes to
    breakeven, after target n it goes to target n-1 ("one step behind"); give-back guard between
    targets; hard stop at the channel's level immediately (no waiting for a 4h close).

Everything money-related goes through ai_pool_service (same ledger, same isolation).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.ai_pool import AiPool, AiPoolPosition, TelegramSignal
from app.models.trading import AppSetting
from app.services import ai_pool_service as pools
from app.services.ai_strategy import Signal
from app.services.telegram_signals import ParsedSignal, looks_like_any_signal, parse_any, validate_signal
from app.services.telegram_signals_v2 import MARKET_ENTRY_TOLERANCE

logger = logging.getLogger(__name__)

LAST_MSG_KEY_PREFIX = "tg_last_msg_id:"


# ── Last-seen message bookkeeping (so restarts never replay old posts) ───────────

def get_last_msg_id(db: Session, channel: str) -> int:
    row = db.query(AppSetting).filter(AppSetting.key == LAST_MSG_KEY_PREFIX + channel).first()
    try:
        return int(row.value) if row and row.value else 0
    except ValueError:
        return 0


def set_last_msg_id(db: Session, channel: str, msg_id: int) -> None:
    key = LAST_MSG_KEY_PREFIX + channel
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    current = get_last_msg_id(db, channel)
    if msg_id <= current:
        return
    if row:
        row.value = str(int(msg_id))
    else:
        db.add(AppSetting(key=key, value=str(int(msg_id))))
    db.flush()


# ── Pool lookup ────────────────────────────────────────────────────────────────

def telegram_pool(db: Session, channel: Optional[str] = None) -> Optional[AiPool]:
    """The pool following `channel`; a pool without a channel is the legacy/default one."""
    q = db.query(AiPool).filter(AiPool.kind == "telegram", AiPool.status != "deleted")
    if channel:
        exact = q.filter(AiPool.channel == channel).order_by(AiPool.id.asc()).first()
        if exact:
            return exact
        return q.filter(AiPool.channel.is_(None)).order_by(AiPool.id.asc()).first()
    return q.order_by(AiPool.id.asc()).first()


def telegram_pools(db: Session) -> list[AiPool]:
    return db.query(AiPool).filter(AiPool.kind == "telegram", AiPool.status != "deleted").order_by(AiPool.id.asc()).all()


# ── Ingest ─────────────────────────────────────────────────────────────────────

def ingest_message(channel: str, msg_id: int, text: str, posted_at: Optional[datetime] = None) -> Optional[dict]:
    """Entry point used by the listener thread. Opens its own session. Returns a status dict or None."""
    db = SessionLocal()
    try:
        return ingest_message_db(db, channel, msg_id, text, posted_at)
    except Exception as exc:
        db.rollback()
        logger.exception("telegram ingest failed for msg %s: %s", msg_id, exc)
        return {"status": "error", "note": str(exc)}
    finally:
        db.close()


def ingest_message_db(db: Session, channel: str, msg_id: int, text: str, posted_at: Optional[datetime] = None) -> Optional[dict]:
    channel = (channel or "").lstrip("@")
    existing = db.query(TelegramSignal).filter(TelegramSignal.channel == channel, TelegramSignal.msg_id == int(msg_id)).first()
    if existing:
        return {"status": existing.status, "note": "duplicate (edit ignored)", "id": existing.id}
    set_last_msg_id(db, channel, int(msg_id))
    if not looks_like_any_signal(text or ""):
        db.commit()
        return None
    parsed = parse_any(text or "")
    row = TelegramSignal(channel=channel, msg_id=int(msg_id), posted_at=posted_at, raw_text=text or "")
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()  # a concurrent ingest of the same post won the race: treat as duplicate
        existing = db.query(TelegramSignal).filter(TelegramSignal.channel == channel, TelegramSignal.msg_id == int(msg_id)).first()
        return {"status": existing.status if existing else "duplicate", "note": "duplicate (race)", "id": existing.id if existing else None}
    if parsed is None:
        row.status = "invalid"
        row.status_note = "could not parse entry/targets"
        db.commit()
        return {"status": row.status, "note": row.status_note, "id": row.id}
    row.symbol = parsed.symbol
    row.entry_low = parsed.entry_low
    row.entry_high = parsed.entry_high
    row.stop_price = parsed.stop_price
    row.entry_kind = getattr(parsed, "entry_kind", "zone") or "zone"
    row.leg2_price = getattr(parsed, "leg2_price", None)
    row.targets_json = json.dumps([{"price": t.price, "pct": t.pct, "fraction": t.sell_fraction, "done": False} for t in parsed.targets])
    problems = validate_signal(parsed)
    if problems:
        row.status = "not_binance" if any("not Binance" in p for p in problems) else "invalid"
        row.status_note = "; ".join(problems)[:240]
        db.commit()
        return {"status": row.status, "note": row.status_note, "id": row.id}
    pool = telegram_pool(db, channel)
    if pool is None:
        row.status = "no_pool"
        row.status_note = f"no Signals pool follows @{channel}"
        db.commit()
        return {"status": row.status, "note": row.status_note, "id": row.id}
    row.pool_id = pool.id
    ex = pools._exchange(pool.account)
    lookup_failed = False
    try:
        filters = ex.get_symbol_lot_filters(parsed.symbol)
    except Exception as exc:
        filters = {}
        lookup_failed = True
        logger.error("lot filter lookup failed for %s (will retry while pending): %s", parsed.symbol, exc)
    if not filters and not lookup_failed:
        row.status = "not_listed"
        row.status_note = f"{parsed.symbol} is not tradable on this account"
        db.commit()
        return {"status": row.status, "note": row.status_note, "id": row.id}
    row.status = "pending_entry"
    row.status_note = "exchange lookup failed; retrying" if lookup_failed else None
    row.expires_at = datetime.utcnow() + timedelta(hours=float(settings.telegram_entry_window_hours))
    db.commit()
    if not lookup_failed:
        _try_enter(db, pool, row)
        db.commit()
    return {"status": row.status, "note": row.status_note, "id": row.id}


# ── Entry ──────────────────────────────────────────────────────────────────────

def _try_enter(db: Session, pool: AiPool, row: TelegramSignal, price: Optional[float] = None) -> bool:
    """
    Attempt to open the position for a pending signal. Decision + sizing happen under the ledger
    lock; the exchange order (pools._buy) runs without it and reserves cash itself. The plan is
    written in the same commit as the position (never a position without its targets).
    """
    if row.status != "pending_entry":
        return False
    if row.expires_at and datetime.utcnow() > row.expires_at:
        row.status = "missed"
        row.status_note = "price never returned to the entry zone within the window"
        pools._log(db, pool.id, "SIGNAL_MISSED", row.status_note, row.symbol)
        return False
    if price is None:
        price = float(pools._prices_for([row.symbol]).get(row.symbol, 0.0))
    if price <= 0:
        return False
    # Sanity: a mis-parsed level (thousands separator, wrong decimal) must never reach an order.
    if price > float(row.entry_high) * 5.0 or price < float(row.entry_low) * 0.2:
        row.status = "invalid"
        row.status_note = f"live price {price:.6g} is far from the parsed zone {row.entry_low:.6g}-{row.entry_high:.6g}; parse rejected"
        pools._log(db, pool.id, "SIGNAL_SKIP", row.status_note, row.symbol)
        return False
    if price <= float(row.stop_price):
        row.status = "invalid"
        row.status_note = f"price {price:.6g} already below the signal stop {row.stop_price:.6g}"
        pools._log(db, pool.id, "SIGNAL_SKIP", row.status_note, row.symbol)
        return False
    tolerance = 1.0 + (MARKET_ENTRY_TOLERANCE if (row.entry_kind or "zone") == "market" else 0.002)
    if price > float(row.entry_high) * tolerance:
        return False  # above the entry: wait (never chase)

    with pools._LEDGER_LOCK:
        db.refresh(pool)
        if pool.status != "running":
            return False  # keep pending; the pool may be resumed within the window
        positions = pools._open_positions(db, pool)
        if any(p.symbol == row.symbol for p in positions):
            row.status = "invalid"
            row.status_note = "already holding this symbol in the pool"
            return False
        slots = max(1, int(pool.max_positions or 5))
        if len(positions) >= slots:
            row.status_note = "all slots busy; waiting"
            return False
        prices = pools._prices_for([p.symbol for p in positions])
        equity = pools.pool_equity(pool, positions, prices)
        min_notional = pools.MIN_NOTIONAL_FLOOR
        try:
            f = pools._exchange(pool.account).get_symbol_lot_filters(row.symbol)
            if not f:
                row.status = "not_listed"
                row.status_note = f"{row.symbol} is not tradable on this account"
                return False
            min_notional = max(min_notional, float(f.get("min_notional", 0.0) or 0.0) * 1.15)
        except Exception as exc:
            logger.error("lot filter lookup failed for %s; retrying later: %s", row.symbol, exc)
            row.status_note = "exchange lookup failed; retrying"
            return False
        spendable = max(0.0, float(pool.cash_usdt) - 0.25)
        amount = min(spendable, max(min_notional, equity / slots))
        if amount < min_notional:
            row.status = "skipped_cash"
            row.status_note = f"pool cash {pool.cash_usdt:.2f} USDT below the minimum order"
            pools._log(db, pool.id, "SIGNAL_SKIP", row.status_note, row.symbol)
            return False
        targets = json.loads(row.targets_json or "[]")
        pools._commit_quietly(db)

    sig = Signal(
        symbol=row.symbol,
        strategy="telegram",
        score=0.0,
        price=price,
        atr_4h=0.0,
        stop_price=float(row.stop_price),
        tp1_price=float(targets[0]["price"]) if targets else price,
        tp1_fraction=float(targets[0]["fraction"]) if targets else 0.0,
        trail_mult=0.0,
        reasons=[f"Telegram @{row.channel} msg {row.msg_id}", f"zone {row.entry_low:.6g}-{row.entry_high:.6g}", f"{len(targets)} targets"],
        metrics={},
    )
    # Split entry ("تقسيم الدخول"): part now, the rest waits at the bottom of the zone.
    split = float(pool.entry_split_pct if pool.entry_split_pct is not None else 50.0) / 100.0
    leg1 = round(amount, 2)
    leg2: Optional[dict] = None
    at_bottom = price <= float(row.entry_low) * 1.002
    leg2_price = float(row.leg2_price) if row.leg2_price else second_leg_price(pool, price, float(row.entry_low), float(row.entry_high))
    if 0.0 < split < 1.0 and not at_bottom and price > leg2_price * 1.002 and amount * split >= min_notional and amount * (1.0 - split) >= min_notional:
        leg1 = round(amount * split, 2)
        fallback_h = float(pool.leg2_fallback_hours if pool.leg2_fallback_hours is not None else 24.0)
        leg2 = {
            "amount": round(amount - leg1, 2),
            "price": leg2_price,
            "level": "channel" if row.leg2_price else str(pool.leg2_level or "mid"),
            "expires_at": (datetime.utcnow() + timedelta(hours=72)).isoformat(timespec="seconds"),
            "fallback_at": (datetime.utcnow() + timedelta(hours=fallback_h)).isoformat(timespec="seconds") if fallback_h > 0 else None,
            "filled": False,
        }
    plan = {"targets": targets, "stop_level": float(row.stop_price), "channel": row.channel, "msg_id": row.msg_id, "entry_low": float(row.entry_low), "entry_high": float(row.entry_high)}
    if leg2:
        plan["leg2"] = leg2
    extra = {
        "tp1_price": sig.tp1_price,
        "trail_atr": 0.0,
        "signal_id": row.id,
        "plan_json": json.dumps(plan),
    }
    pos = pools._buy(db, pool, sig, leg1, extra=extra)  # no lock held here
    if pos is None:
        row.status_note = "buy failed; will retry while pending"
        return False
    with pools._LEDGER_LOCK:
        row.status = "entered"
        row.position_id = pos.id
        row.status_note = f"bought {leg1:.2f} USDT @ {pos.avg_entry:.6g}" + (f"; {leg2['amount']:.2f} USDT waits at {leg2['price']:.6g}" if leg2 else "")
        pools._commit_quietly(db)
    return True


def second_leg_price(pool: AiPool, leg1_price: float, entry_low: float, entry_high: float) -> float:
    """Where the second entry leg waits, per pool setting. Never below the zone bottom."""
    level = str(pool.leg2_level or "mid")
    if level == "bottom":
        return float(entry_low)
    if level == "below_pct":
        pct = float(pool.leg2_below_pct if pool.leg2_below_pct is not None else 3.0)
        return max(float(entry_low), leg1_price * (1.0 - pct / 100.0))
    return (float(entry_low) + float(entry_high)) / 2.0  # mid


def _fill_second_leg(db: Session, pool: AiPool, pos: AiPoolPosition, plan: dict, price: float) -> None:
    """Buy the waiting half at its level, or at market once the fallback time passes while still in zone (no target yet)."""
    leg2 = plan.get("leg2") or {}
    if not leg2 or leg2.get("filled") or leg2.get("cancelled"):
        return
    targets = plan.get("targets", [])
    if any(t.get("done") for t in targets):
        leg2["cancelled"] = "first target already hit"
        pos.plan_json = json.dumps(plan)
        return
    try:
        expires = datetime.fromisoformat(str(leg2.get("expires_at")))
    except ValueError:
        expires = datetime.utcnow()
    if datetime.utcnow() > expires:
        leg2["cancelled"] = "expired"
        pos.plan_json = json.dumps(plan)
        pools._log(db, pool.id, "LEG2_CANCELLED", "second entry leg expired unfilled", pos.symbol)
        return
    if price <= float(pos.stop_price):
        return
    at_level = price <= float(leg2["price"]) * 1.002
    fallback_due = False
    if leg2.get("fallback_at"):
        try:
            fallback_due = datetime.utcnow() >= datetime.fromisoformat(str(leg2["fallback_at"]))
        except ValueError:
            fallback_due = False
    entry_high = float(plan.get("entry_high") or 0.0)
    in_zone = entry_high <= 0 or price <= entry_high * 1.002
    if not at_level and not (fallback_due and in_zone):
        return
    reason = "second entry leg at its level" if at_level else "second entry leg: fallback at market (still in zone, no target yet)"
    sig = Signal(symbol=pos.symbol, strategy="telegram", score=0.0, price=price, atr_4h=0.0, stop_price=float(pos.stop_price), tp1_price=float(pos.tp1_price or price), tp1_fraction=0.0, trail_mult=0.0, reasons=[reason], metrics={})
    res = pools._buy(db, pool, sig, float(leg2["amount"]), merge_into=pos)
    if res is None:
        return
    leg2["filled"] = True
    leg2["filled_at"] = datetime.utcnow().isoformat(timespec="seconds")
    leg2["fill_price"] = price
    pos.plan_json = json.dumps(plan)
    _note_signal(db, pos, f"second leg filled @ {price:.6g}; avg {float(pos.avg_entry):.6g}")
    pools._commit_quietly(db)


def repair_unlinked_positions(db: Session, pool: AiPool) -> int:
    """Safety net: a telegram position without a plan/signal link (should never happen) is re-linked and logged."""
    orphans = db.query(AiPoolPosition).filter(AiPoolPosition.pool_id == pool.id, AiPoolPosition.status == "open", AiPoolPosition.strategy == "telegram", AiPoolPosition.signal_id.is_(None)).all()
    fixed = 0
    for pos in orphans:
        row = db.query(TelegramSignal).filter(TelegramSignal.pool_id == pool.id, TelegramSignal.symbol == pos.symbol, TelegramSignal.status.in_(["pending_entry", "entered"])).order_by(desc(TelegramSignal.id)).first()
        if row is None:
            pools._log(db, pool.id, "ERROR", f"position {pos.id} has no signal link and no matching signal; target ladder unavailable, stop still active", pos.symbol)
            continue
        pos.signal_id = row.id
        pos.plan_json = json.dumps({"targets": json.loads(row.targets_json or "[]"), "stop_level": float(row.stop_price or pos.stop_price), "channel": row.channel, "msg_id": row.msg_id})
        row.status = "entered"
        row.position_id = pos.id
        pools._log(db, pool.id, "ERROR", f"position {pos.id} was not linked to its signal (msg {row.msg_id}); repaired", pos.symbol)
        fixed += 1
    return fixed


def check_pending_signals(db: Session, pool: AiPool) -> int:
    """Called by the scan loop (every ~5 min) for telegram pools: retry pending entries, expire old ones."""
    try:
        if repair_unlinked_positions(db, pool):
            db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("repair pass failed: %s", exc)
    rows = db.query(TelegramSignal).filter(TelegramSignal.pool_id == pool.id, TelegramSignal.status == "pending_entry").all()
    if not rows:
        return 0
    prices = pools._prices_for([r.symbol for r in rows])
    entered = 0
    for row in rows:
        try:
            if _try_enter(db, pool, row, float(prices.get(row.symbol, 0.0))):
                entered += 1
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.exception("pending signal %s failed: %s", row.id, exc)
    return entered


# ── Exits (called from the pool tick for strategy == 'telegram') ───────────────

def manage_signal_position(db: Session, pool: AiPool, pos: AiPoolPosition, price: float, balances: Optional[dict] = None, defer_full_exits: bool = False) -> Optional[str]:
    pos.current_price = price
    pos.highest_price = max(float(pos.highest_price or 0.0), price)
    invested = float(pos.invested_usdt)
    pos.unrealized_pnl_usdt = float(pos.qty) * price - invested
    pos.unrealized_pnl_pct = (price / float(pos.avg_entry) - 1.0) * 100.0 if float(pos.avg_entry) > 0 else 0.0

    try:
        plan = json.loads(pos.plan_json or "{}")
    except (ValueError, TypeError) as exc:
        logger.error("position %s (%s): corrupt plan_json, target ladder disabled, stop still active: %s", pos.id, pos.symbol, exc)
        plan = {}
    targets: list[dict] = plan.get("targets", [])
    done_count = sum(1 for t in targets if t.get("done"))

    # 1) Hard stop (channel level, or the raised level after targets)
    if price <= float(pos.stop_price):
        kind = "trail" if done_count > 0 else "stop"
        if defer_full_exits:
            return kind
        leg2 = plan.get("leg2")
        if leg2 and not leg2.get("filled") and not leg2.get("cancelled"):
            leg2["cancelled"] = "stopped out"
            pos.plan_json = json.dumps(plan)
        pools._sell(db, pool, pos, float(pos.qty), kind, price, balances)
        return None  # status sync happens in _close_position_record

    # 1b) Second entry leg waiting at the bottom of the zone
    if plan.get("leg2"):
        _fill_second_leg(db, pool, pos, plan, price)

    # 2) Targets, in order; every target the price has cleared is filled this tick
    min_notional = _min_notional_for(pool, pos.symbol)
    for i, t in enumerate(targets):
        if t.get("done"):
            continue
        if price < float(t["price"]):
            break
        is_last = i == len(targets) - 1
        if is_last:
            # Last target: sell only part of its fraction; the rest is a "runner" that trails.
            last_sell = float(pool.last_target_sell_pct if pool.last_target_sell_pct is not None else 50.0) / 100.0
            qty = min(float(pos.qty), float(pos.qty_initial) * float(t.get("fraction", 0.0)) * last_sell)
            if qty * price < min_notional * 1.1 or (float(pos.qty) - qty) * price < min_notional * 1.1:
                # too small to split at the exchange minimum: keep everything as runner (no sale) unless
                # even the whole remainder is small, then sell all.
                qty = float(pos.qty) if float(pos.qty) * price < min_notional * 2.2 else 0.0
        else:
            qty = min(float(pos.qty), float(pos.qty_initial) * float(t.get("fraction", 0.0)))
            # Binance rejects orders under the minimum notional (5 USDT). With small slots the channel's
            # 20% slice can be worth 4 USDT: sell the minimum instead, and sell everything when the
            # remainder would itself become unsellable dust.
            floor_qty = (min_notional * 1.1) / price if price > 0 else qty
            qty = min(float(pos.qty), max(qty, floor_qty))
            if (float(pos.qty) - qty) * price < min_notional * 1.1:
                qty = float(pos.qty)
        res = None
        if qty > 0:
            res = pools._sell(db, pool, pos, qty, f"tp{i + 1}", price, balances)
            if res is None and pos.status == "open":
                _note_signal(db, pos, f"target {i + 1} sell failing; retrying next tick")
                pools._commit_quietly(db)
                break
        t["done"] = True
        t["done_at"] = datetime.utcnow().isoformat(timespec="seconds")
        t["fill_price"] = float(res["avg"]) if res else price
        leg2 = plan.get("leg2")
        if leg2 and not leg2.get("filled") and not leg2.get("cancelled"):
            leg2["cancelled"] = "first target hit"  # never add size to a trade that is already taking profit
        if pos.status == "open":
            # Stop after target n: previous level (entry for n=1) + target_lock_pct of the leg to target n.
            prev_level = float(pos.avg_entry) if i == 0 else float(targets[i - 1]["price"])
            lock = float(pool.target_lock_pct if pool.target_lock_pct is not None else 50.0) / 100.0
            new_stop = prev_level + lock * (float(t["price"]) - prev_level)
            if lock >= 0.999:
                new_stop = float(t["price"]) * 0.997  # "at the target": 0.3% buffer so the touch itself never triggers it
            new_stop = max(new_stop, pools.breakeven_price(float(pos.avg_entry), settings.trading_fee_pct))
            if new_stop > float(pos.stop_price):
                pos.stop_price = new_stop
                pools._log(db, pool.id, "STOP_MOVE", f"target {i + 1} hit: stop raised to {new_stop:.6g} ({lock * 100:.0f}% of the leg above {prev_level:.6g})", pos.symbol)
            pos.tp1_done = True
        pos.plan_json = json.dumps(plan)
        sync_signal_status(db, pos)
        pools._commit_quietly(db)
        if pos.status != "open":
            return None
        # keep looping: a gap may have cleared several targets at once

    if pos.status != "open":
        return None

    # 3) Give-back guard after the first target; tighter for the runner once every target is done.
    done_count = sum(1 for t in targets if t.get("done"))
    if done_count > 0:
        giveback = float(pool.profit_giveback_pct if pool.profit_giveback_pct is not None else 50.0)
        if targets and done_count >= len(targets):
            giveback = min(giveback, float(pool.runner_giveback_pct if pool.runner_giveback_pct is not None else 30.0))
        from app.services.ai_strategy import btc_short_term_bias  # lazy import (test-patchable via pools)
        if pools.btc_short_term_bias() == "weak":
            giveback = min(giveback, pools.WEAK_BTC_MAX_GIVEBACK_PCT)
        peak_gain = float(pos.highest_price) - float(pos.avg_entry)
        if peak_gain > 0 and giveback < 100.0:
            guard = float(pos.avg_entry) + peak_gain * (1.0 - giveback / 100.0)
            if guard > float(pos.stop_price):
                pos.stop_price = guard
    return None


def _min_notional_for(pool: AiPool, symbol: str) -> float:
    try:
        f = pools._exchange(pool.account).get_symbol_lot_filters(symbol)
        return max(5.0, float(f.get("min_notional", 0.0) or 0.0))
    except Exception as exc:
        logger.warning("min_notional lookup failed for %s, using 5 USDT: %s", symbol, exc)
        return 5.0


def _note_signal(db: Session, pos: AiPoolPosition, note: str) -> None:
    if not pos.signal_id:
        return
    row = db.query(TelegramSignal).filter(TelegramSignal.id == pos.signal_id).first()
    if row:
        row.status_note = note[:240]


def sync_signal_status(db: Session, pos: AiPoolPosition) -> None:
    if not pos.signal_id:
        return
    row = db.query(TelegramSignal).filter(TelegramSignal.id == pos.signal_id).first()
    if not row:
        return
    if pos.status == "closed":
        row.status = "closed"
        row.status_note = f"closed ({pos.close_reason}) pnl {float(pos.realized_pnl_usdt or 0.0):+.2f} USDT"
    else:
        try:
            targets = json.loads(pos.plan_json or "{}").get("targets", [])
        except (ValueError, TypeError) as exc:
            logger.error("position %s: corrupt plan_json while syncing status: %s", pos.id, exc)
            targets = []
        done = sum(1 for t in targets if t.get("done"))
        row.status_note = f"{done}/{len(targets)} targets hit, stop {float(pos.stop_price):.6g}"


# ── Read models ────────────────────────────────────────────────────────────────

def recent_signals(db: Session, limit: int = 60, channel: Optional[str] = None) -> list[dict]:
    q = db.query(TelegramSignal)
    if channel:
        q = q.filter(TelegramSignal.channel == channel)
    rows = q.order_by(desc(TelegramSignal.id)).limit(limit).all()
    out = []
    for r in rows:
        try:
            targets = json.loads(r.targets_json or "[]")
        except ValueError:
            targets = []
        out.append({
            "id": r.id,
            "msg_id": r.msg_id,
            "posted_at": r.posted_at.strftime("%m-%d %H:%M") if r.posted_at else "",
            "created_at": r.created_at.strftime("%m-%d %H:%M") if r.created_at else "",
            "symbol": r.symbol or "-",
            "channel": r.channel,
            "entry_kind": r.entry_kind or "zone",
            "leg2_price": r.leg2_price,
            "entry_low": r.entry_low,
            "entry_high": r.entry_high,
            "stop_price": r.stop_price,
            "targets": targets,
            "status": r.status,
            "status_note": r.status_note or "",
            "position_id": r.position_id,
            "raw_text": r.raw_text or "",
        })
    return out


def plan_for_position(pos: AiPoolPosition) -> dict:
    try:
        plan = json.loads(pos.plan_json or "{}")
    except ValueError:
        plan = {}
    entry = float(pos.avg_entry or 0.0)
    targets = plan.get("targets", [])
    for t in targets:
        t["pct_from_entry"] = (float(t["price"]) / entry - 1.0) * 100.0 if entry > 0 else 0.0
    leg2 = plan.get("leg2") or None
    leg2_text = ""
    if leg2:
        if leg2.get("filled"):
            leg2_text = f"2nd leg filled @ {float(leg2.get('fill_price', 0.0)):.6g}"
        elif leg2.get("cancelled"):
            leg2_text = f"2nd leg cancelled ({leg2.get('cancelled')})"
        else:
            fb = str(leg2.get("fallback_at") or "")[:16].replace("T", " ")
            leg2_text = f"2nd leg {float(leg2.get('amount', 0.0)):.2f} USDT waiting at {float(leg2.get('price', 0.0)):.6g} ({leg2.get('level', 'mid')})" + (f", or at market after {fb} UTC if still in zone" if fb else "")
    return {"targets": targets, "stop_level": plan.get("stop_level"), "msg_id": plan.get("msg_id"), "leg2": leg2, "leg2_text": leg2_text}
