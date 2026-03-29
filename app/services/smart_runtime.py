from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.paper_v2 import (
    ActivityLog,
    Campaign,
    DcaRule,
    MarketSnapshot,
    Position,
    PositionDcaState,
    SmartRuntimeState,
)
from app.services.binance_public import get_klines
from app.services.live_trading import recalculate_live_campaign_dca
from app.services.paper_trading import _ema, _support_engine, build_smart_dca_plan, recalculate_campaign_dca


def _latest_symbol_for_campaign(db: Session, campaign_id: int) -> str | None:
    row = (
        db.query(Position.symbol)
        .filter(Position.campaign_id == campaign_id)
        .order_by(Position.id.desc())
        .first()
    )
    if row and row[0]:
        return str(row[0]).upper()
    return None


def _strategy_mode_from_campaign(campaign: Campaign, symbol: str) -> str:
    profile = str(campaign.ai_dca_profile or "").strip().lower()
    if "auto" in profile:
        try:
            state, _, _ = _simple_state(symbol)
            if state == "bullish":
                return "aggressive"
            if state == "sideways":
                return "balanced"
            return "conservative"
        except Exception:
            return "balanced"
    if "aggressive" in profile:
        return "aggressive"
    if "conservative" in profile:
        return "conservative"
    return "balanced"


def _simple_state(symbol: str) -> tuple[str, float, float]:
    kl = get_klines(symbol, "4h", 260)
    closes = [float(k[4]) for k in kl]
    if len(closes) < 200:
        return "sideways", float(closes[-1]) if closes else 0.0, 0.0
    ema200 = _ema(closes, 200) or 0.0
    price = closes[-1]
    if ema200 <= 0:
        return "sideways", float(price), 0.0
    if price > ema200:
        return "bullish", float(price), float(ema200)
    if abs(price - ema200) / ema200 < 0.05:
        return "sideways", float(price), float(ema200)
    return "bearish", float(price), float(ema200)


def _upsert_runtime(
    db: Session,
    campaign: Campaign,
    symbol: str,
) -> SmartRuntimeState:
    st = db.query(SmartRuntimeState).filter(SmartRuntimeState.campaign_id == campaign.id).first()
    if not st:
        st = SmartRuntimeState(
            campaign_id=campaign.id,
            symbol=symbol,
            mode=campaign.mode,
            execution_zones_locked=True,
        )
        db.add(st)
        db.flush()
    else:
        st.symbol = symbol
        st.mode = campaign.mode
    return st


def refresh_smart_medium(db: Session) -> tuple[int, int]:
    campaigns = db.query(Campaign).filter(Campaign.smart_dca_enabled == True).all()
    touched = 0
    errors = 0
    now = datetime.utcnow()
    for c in campaigns:
        symbol = _latest_symbol_for_campaign(db, c.id)
        if not symbol:
            continue
        try:
            market_state, price, ema200 = _simple_state(symbol)
            st = _upsert_runtime(db, c, symbol)
            st.market_state = market_state
            st.current_price = price
            st.ema200 = ema200
            st.last_medium_refresh_at = now

            db.add(
                MarketSnapshot(
                    campaign_id=c.id,
                    symbol=symbol,
                    mode=c.mode,
                    market_state=market_state,
                    price=price,
                    ema200=ema200,
                )
            )
            touched += 1
        except Exception as e:
            errors += 1
            db.add(
                ActivityLog(
                    event_type="SMART_MEDIUM_FAIL",
                    symbol=symbol,
                    message=f"Campaign={c.name} | error={e}",
                )
            )
    if touched or errors:
        db.commit()
    return touched, errors


def _replace_campaign_rules_with_plan(db: Session, campaign: Campaign, plan_rules: list[dict]) -> None:
    incoming = []
    for row in plan_rules:
        drop = float(row.get("drop_pct", 0.0))
        alloc = float(row.get("allocation_pct", 0.0))
        if drop <= 0 or alloc <= 0:
            continue
        incoming.append((str(row.get("name", "SMART-DCA")).strip() or "SMART-DCA", drop, alloc))

    existing = {r.name: r for r in db.query(DcaRule).filter(DcaRule.campaign_id == campaign.id).all()}
    keep_names: set[str] = set()
    for n, d, a in incoming:
        keep_names.add(n)
        r = existing.get(n)
        if r:
            r.drop_pct = d
            r.allocation_pct = a
        else:
            db.add(DcaRule(campaign_id=campaign.id, name=n, drop_pct=d, allocation_pct=a))
    for n, r in existing.items():
        if n not in keep_names:
            db.delete(r)


def refresh_smart_slow(db: Session) -> tuple[int, int]:
    campaigns = db.query(Campaign).filter(Campaign.smart_dca_enabled == True).all()
    touched = 0
    review_only = 0
    now = datetime.utcnow()
    for c in campaigns:
        symbol = _latest_symbol_for_campaign(db, c.id)
        if not symbol:
            continue
        mode = _strategy_mode_from_campaign(c, symbol)
        plan = build_smart_dca_plan(
            symbol=symbol,
            entry_amount_usdt=float(c.entry_amount_usdt),
            tp_pct=c.tp_pct,
            sl_pct=c.sl_pct,
            max_levels=6,
            strategy_mode=mode,
        )
        if not plan.get("ok"):
            db.add(
                ActivityLog(
                    event_type="SMART_SLOW_FAIL",
                    symbol=symbol,
                    message=f"Campaign={c.name} | error={plan.get('error','plan failed')}",
                )
            )
            continue

        st = _upsert_runtime(db, c, symbol)
        st.dynamic_zones_json = json.dumps(plan.get("rules", []))
        st.last_slow_recalc_at = now

        support_ctx = _support_engine(symbol)
        breakdown = bool((support_ctx or {}).get("breakdown_suspected", False))
        has_dca_execution = (
            db.query(PositionDcaState.id)
            .join(Position, Position.id == PositionDcaState.position_id)
            .filter(Position.campaign_id == c.id, PositionDcaState.executed == True)
            .first()
            is not None
        )

        if breakdown:
            st.recalc_recommended = True
            st.recalc_reason = "structure_break_detected"
            db.add(
                ActivityLog(
                    event_type="SMART_REVIEW",
                    symbol=symbol,
                    message=f"Campaign={c.name} | reason=structure_break_detected",
                )
            )
            review_only += 1
            continue

        if has_dca_execution:
            st.recalc_recommended = True
            st.recalc_reason = "dca_already_executed"
            db.add(
                ActivityLog(
                    event_type="SMART_REVIEW",
                    symbol=symbol,
                    message=f"Campaign={c.name} | reason=dca_already_executed | execution_zones_locked=1",
                )
            )
            review_only += 1
            continue

        # Safe auto-apply only when no DCA execution happened yet.
        _replace_campaign_rules_with_plan(db, c, list(plan.get("rules", [])))
        c.ai_dca_suggested_rules_json = json.dumps(plan.get("rules", []))
        st.recalc_recommended = False
        st.recalc_reason = None
        st.execution_zones_locked = True
        db.flush()
        if c.mode == "live":
            recalculate_live_campaign_dca(db, c)
        else:
            recalculate_campaign_dca(db, c)
        db.add(
            ActivityLog(
                event_type="SMART_RECALC_APPLY",
                symbol=symbol,
                message=f"Campaign={c.name} | mode={c.mode} | strategy={mode} | rules={len(plan.get('rules', []))}",
            )
        )
        touched += 1

    if touched or review_only:
        db.commit()
    return touched, review_only
