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
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.ai_pool import AiPool, AiPoolPosition, TelegramSignal
from app.models.trading import AppSetting
from app.services import ai_pool_service as pools
from app.services.ai_strategy import Signal
from app.services.telegram_signals import ParsedSignal, looks_like_signal, parse_signal, validate_signal

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

def telegram_pool(db: Session) -> Optional[AiPool]:
    return db.query(AiPool).filter(AiPool.kind == "telegram", AiPool.status != "deleted").order_by(AiPool.id.asc()).first()


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
    if not looks_like_signal(text or ""):
        db.commit()
        return None
    parsed = parse_signal(text or "")
    row = TelegramSignal(channel=channel, msg_id=int(msg_id), posted_at=posted_at, raw_text=text or "")
    db.add(row)
    db.flush()
    if parsed is None:
        row.status = "invalid"
        row.status_note = "could not parse entry/targets"
        db.commit()
        return {"status": row.status, "note": row.status_note, "id": row.id}
    row.symbol = parsed.symbol
    row.entry_low = parsed.entry_low
    row.entry_high = parsed.entry_high
    row.stop_price = parsed.stop_price
    row.targets_json = json.dumps([{"price": t.price, "pct": t.pct, "fraction": t.sell_fraction, "done": False} for t in parsed.targets])
    problems = validate_signal(parsed)
    if problems:
        row.status = "not_binance" if any("not Binance" in p for p in problems) else "invalid"
        row.status_note = "; ".join(problems)[:240]
        db.commit()
        return {"status": row.status, "note": row.status_note, "id": row.id}
    pool = telegram_pool(db)
    if pool is None:
        row.status = "no_pool"
        row.status_note = "no Signals pool exists"
        db.commit()
        return {"status": row.status, "note": row.status_note, "id": row.id}
    row.pool_id = pool.id
    ex = pools._exchange(pool.account)
    try:
        filters = ex.get_symbol_lot_filters(parsed.symbol)
    except Exception as exc:
        filters = {}
        logger.warning("lot filter lookup failed for %s: %s", parsed.symbol, exc)
    if not filters:
        row.status = "not_listed"
        row.status_note = f"{parsed.symbol} is not tradable on this account"
        db.commit()
        return {"status": row.status, "note": row.status_note, "id": row.id}
    row.status = "pending_entry"
    row.expires_at = datetime.utcnow() + timedelta(hours=float(settings.telegram_entry_window_hours))
    db.commit()
    with pools._LEDGER_LOCK:
        db.refresh(pool)
        _try_enter(db, pool, row)
        db.commit()
    return {"status": row.status, "note": row.status_note, "id": row.id}


# ── Entry ──────────────────────────────────────────────────────────────────────

def _try_enter(db: Session, pool: AiPool, row: TelegramSignal, price: Optional[float] = None) -> bool:
    """Attempt to open the position for a pending signal. Caller holds the ledger lock."""
    if row.status != "pending_entry":
        return False
    if row.expires_at and datetime.utcnow() > row.expires_at:
        row.status = "missed"
        row.status_note = "price never returned to the entry zone within the window"
        pools._log(db, pool.id, "SIGNAL_MISSED", row.status_note, row.symbol)
        return False
    if pool.status != "running":
        return False  # keep pending; the pool may be resumed within the window
    if price is None:
        price = float(pools._prices_for([row.symbol]).get(row.symbol, 0.0))
    if price <= 0:
        return False
    if price <= float(row.stop_price):
        row.status = "invalid"
        row.status_note = f"price {price:.6g} already below the signal stop {row.stop_price:.6g}"
        pools._log(db, pool.id, "SIGNAL_SKIP", row.status_note, row.symbol)
        return False
    if price > float(row.entry_high) * 1.002:
        return False  # above the zone: wait (never chase)

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
        min_notional = max(min_notional, float(f.get("min_notional", 0.0) or 0.0) * 1.15)
    except Exception:
        pass
    spendable = max(0.0, float(pool.cash_usdt) - 0.25)
    amount = min(spendable, max(min_notional, equity / slots))
    if amount < min_notional:
        row.status = "skipped_cash"
        row.status_note = f"pool cash {pool.cash_usdt:.2f} USDT below the minimum order"
        pools._log(db, pool.id, "SIGNAL_SKIP", row.status_note, row.symbol)
        return False

    targets = json.loads(row.targets_json or "[]")
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
    pos = pools._buy(db, pool, sig, round(amount, 2))
    if pos is None:
        row.status_note = "buy failed; will retry while pending"
        return False
    pos.tp1_price = sig.tp1_price
    pos.trail_atr = 0.0
    pos.signal_id = row.id
    pos.plan_json = json.dumps({"targets": targets, "stop_level": float(row.stop_price), "channel": row.channel, "msg_id": row.msg_id})
    row.status = "entered"
    row.position_id = pos.id
    row.status_note = f"bought {amount:.2f} USDT @ {pos.avg_entry:.6g}"
    return True


def check_pending_signals(db: Session, pool: AiPool) -> int:
    """Called by the scan loop (every ~5 min) for telegram pools: retry pending entries, expire old ones."""
    rows = db.query(TelegramSignal).filter(TelegramSignal.pool_id == pool.id, TelegramSignal.status == "pending_entry").all()
    if not rows:
        return 0
    prices = pools._prices_for([r.symbol for r in rows])
    entered = 0
    with pools._LEDGER_LOCK:
        db.refresh(pool)
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
    except ValueError:
        plan = {}
    targets: list[dict] = plan.get("targets", [])
    done_count = sum(1 for t in targets if t.get("done"))

    # 1) Hard stop (channel level, or the raised level after targets)
    if price <= float(pos.stop_price):
        kind = "trail" if done_count > 0 else "stop"
        if defer_full_exits:
            return kind
        pools._sell(db, pool, pos, float(pos.qty), kind, price, balances)
        _sync_signal_status(db, pos)
        return None

    # 2) Targets, one per tick, in order
    min_notional = _min_notional_for(pool, pos.symbol)
    for i, t in enumerate(targets):
        if t.get("done"):
            continue
        if price < float(t["price"]):
            break
        is_last = i == len(targets) - 1
        qty = float(pos.qty) if is_last else min(float(pos.qty), float(pos.qty_initial) * float(t.get("fraction", 0.0)))
        if not is_last:
            # Binance rejects orders under the minimum notional (5 USDT). With small slots the channel's
            # 20% slice can be worth 4 USDT: sell the minimum instead, and sell everything when the
            # remainder would itself become unsellable dust.
            floor_qty = (min_notional * 1.1) / price if price > 0 else qty
            qty = min(float(pos.qty), max(qty, floor_qty))
            if (float(pos.qty) - qty) * price < min_notional * 1.1:
                qty = float(pos.qty)
        if qty <= 0:
            t["done"] = True
            continue
        res = pools._sell(db, pool, pos, qty, f"tp{i + 1}", price, balances)
        if res is None and pos.status == "open":
            break  # could not sell right now; retry next tick
        t["done"] = True
        t["done_at"] = datetime.utcnow().isoformat(timespec="seconds")
        t["fill_price"] = float(res["avg"]) if res else price
        if pos.status == "open":
            new_stop = pools.breakeven_price(float(pos.avg_entry), settings.trading_fee_pct) if i == 0 else float(targets[i - 1]["price"])
            if new_stop > float(pos.stop_price):
                pos.stop_price = new_stop
                pools._log(db, pool.id, "STOP_MOVE", f"target {i + 1} hit: stop raised to {new_stop:.6g} ({'breakeven' if i == 0 else 'target ' + str(i)})", pos.symbol)
            pos.tp1_done = True
        pos.plan_json = json.dumps(plan)
        _sync_signal_status(db, pos)
        pools._commit_quietly(db)
        break

    if pos.status != "open":
        return None

    # 3) Give-back guard between targets (after the first target is banked)
    done_count = sum(1 for t in targets if t.get("done"))
    if done_count > 0:
        giveback = float(pool.profit_giveback_pct if pool.profit_giveback_pct is not None else 50.0)
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
    except Exception:
        return 5.0


def _sync_signal_status(db: Session, pos: AiPoolPosition) -> None:
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
        except ValueError:
            targets = []
        done = sum(1 for t in targets if t.get("done"))
        row.status_note = f"{done}/{len(targets)} targets hit, stop {float(pos.stop_price):.6g}"


# ── Read models ────────────────────────────────────────────────────────────────

def recent_signals(db: Session, limit: int = 60) -> list[dict]:
    rows = db.query(TelegramSignal).order_by(desc(TelegramSignal.id)).limit(limit).all()
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
    return {"targets": targets, "stop_level": plan.get("stop_level"), "msg_id": plan.get("msg_id")}
