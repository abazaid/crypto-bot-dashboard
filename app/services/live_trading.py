from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.paper_v2 import ActivityLog, Campaign, DcaRule, Position, PositionDcaState
from app.services.binance_live import get_balances, get_usdt_free, place_market_buy_quote, place_market_sell_qty
from app.services.binance_public import get_prices
from app.services.paper_trading import (
    _ai_dca_confirm,
    btc_market_state,
    build_symbol_ai_dca_rules,
)


def add_live_log(db: Session, event_type: str, symbol: str, message: str) -> None:
    db.add(ActivityLog(event_type=event_type, symbol=symbol or "-", message=message))


def live_wallet_snapshot(db: Session) -> dict:
    balances = get_balances()
    usdt_free = float(balances.get("USDT", {}).get("free", 0.0))
    open_positions = db.query(Position).join(Campaign, Campaign.id == Position.campaign_id).filter(
        Position.status == "open", Campaign.mode == "live"
    ).all()
    symbols = sorted({p.symbol for p in open_positions})
    prices = get_prices(symbols) if symbols else {}
    invested_open = sum(float(p.total_invested_usdt) for p in open_positions)
    market_value = sum(float(prices.get(p.symbol, p.average_price)) * float(p.total_qty) for p in open_positions)
    unrealized = market_value - invested_open
    closed = db.query(Position).join(Campaign, Campaign.id == Position.campaign_id).filter(
        Position.status == "closed", Campaign.mode == "live"
    ).all()
    realized = sum(float(p.realized_pnl_usdt or 0.0) for p in closed)
    equity = usdt_free + market_value
    return {
        "cash": usdt_free,
        "invested_open": invested_open,
        "market_value": market_value,
        "unrealized_pnl": unrealized,
        "realized_pnl": realized,
        "equity": equity,
    }


def _open_live_position(
    db: Session,
    campaign: Campaign,
    symbol: str,
    rules: list[DcaRule],
    rules_by_name: dict[str, DcaRule],
    fallback_ai_rules: list[tuple[str, float, float]],
    ai_profile: str,
    event_type: str,
    event_label: str,
) -> bool:
    order = place_market_buy_quote(symbol, campaign.entry_amount_usdt)
    qty = float(order["executed_qty"])
    spent = float(order["quote_qty"])
    avg = float(order["avg_price"])
    if qty <= 0 or spent <= 0:
        return False

    pos = Position(
        campaign_id=campaign.id,
        symbol=symbol,
        initial_price=avg,
        initial_qty=qty,
        total_invested_usdt=spent,
        total_qty=qty,
        average_price=avg,
    )
    db.add(pos)
    db.flush()

    if campaign.ai_dca_enabled:
        symbol_rules = build_symbol_ai_dca_rules(symbol, ai_profile, fallback_ai_rules, campaign.sl_pct)
        for name_rule, drop_pct, alloc_pct, support_score in symbol_rules:
            rule_ref = rules_by_name.get(name_rule) or (rules[0] if rules else None)
            if not rule_ref:
                continue
            db.add(
                PositionDcaState(
                    position_id=pos.id,
                    dca_rule_id=rule_ref.id,
                    executed=False,
                    custom_drop_pct=drop_pct,
                    custom_allocation_pct=alloc_pct,
                    custom_support_score=support_score,
                )
            )
    else:
        for rule in rules:
            db.add(PositionDcaState(position_id=pos.id, dca_rule_id=rule.id, executed=False))

    add_live_log(
        db,
        event_type,
        symbol,
        f"Live Campaign={campaign.name} | {event_label} at {avg:.6f} | Qty={qty:.8f} | USDT={spent:.2f}",
    )
    return True


def create_live_campaign_positions(db: Session, campaign: Campaign, symbols: list[str]) -> tuple[int, list[str]]:
    picked = sorted(set([s.strip().upper() for s in symbols if s and s.strip()]))
    if not picked:
        return 0, ["No symbols selected."]
    usdt_free = get_usdt_free()
    needed = campaign.entry_amount_usdt * len(picked)
    if usdt_free < needed:
        return 0, [f"Insufficient live USDT. Need {needed:.2f}, have {usdt_free:.2f}."]

    rules = (
        db.query(DcaRule)
        .filter(DcaRule.campaign_id == campaign.id)
        .order_by(DcaRule.drop_pct.asc(), DcaRule.id.asc())
        .all()
    )
    rules_by_name = {r.name: r for r in rules}
    fallback_ai_rules = [(r.name, float(r.drop_pct), float(r.allocation_pct)) for r in rules]
    ai_profile = campaign.ai_dca_profile or "neutral"

    opened = 0
    for symbol in picked:
        try:
            ok = _open_live_position(
                db,
                campaign,
                symbol,
                rules,
                rules_by_name,
                fallback_ai_rules,
                ai_profile,
                "LIVE_OPEN",
                "Initial buy",
            )
            if ok:
                opened += 1
        except Exception as e:
            add_live_log(db, "LIVE_OPEN_FAIL", symbol, f"Campaign={campaign.name} | error={e}")
    db.commit()
    return opened, []


def run_live_cycle(db: Session) -> None:
    campaigns = db.query(Campaign).filter(Campaign.mode == "live").all()
    if not campaigns:
        return
    open_positions = (
        db.query(Position)
        .join(Campaign, Campaign.id == Position.campaign_id)
        .filter(Position.status == "open", Campaign.mode == "live")
        .all()
    )
    if not open_positions:
        return

    prices = get_prices(sorted({p.symbol for p in open_positions}))
    now = datetime.utcnow()
    ai_filter_cache: dict[tuple[str, float], tuple[bool, bool, str]] = {}
    btc_state = btc_market_state()
    changed = False

    for pos in open_positions:
        price = float(prices.get(pos.symbol, 0.0))
        if price <= 0:
            continue
        campaign = pos.campaign

        tp_hit = campaign.tp_pct is not None and price >= (pos.average_price * (1 + (campaign.tp_pct / 100.0)))
        sl_hit = campaign.sl_pct is not None and price <= (pos.average_price * (1 - (campaign.sl_pct / 100.0)))
        if tp_hit or sl_hit:
            try:
                sell = place_market_sell_qty(pos.symbol, pos.total_qty)
                proceeds = float(sell["quote_qty"])
                close_price = float(sell["avg_price"] or price)
            except Exception as e:
                add_live_log(db, "LIVE_CLOSE_FAIL", pos.symbol, f"Campaign={campaign.name} | error={e}")
                continue
            pnl = proceeds - pos.total_invested_usdt
            pos.status = "closed"
            pos.closed_at = now
            pos.close_price = close_price
            pos.realized_pnl_usdt = pnl
            pos.close_reason = "TP" if tp_hit else "SL"
            add_live_log(
                db,
                "LIVE_CLOSE",
                pos.symbol,
                (
                    f"Campaign={campaign.name} | Reason={pos.close_reason} | Close={close_price:.6f} "
                    f"| Invested={pos.total_invested_usdt:.2f} | Proceeds={proceeds:.2f} | PnL={pnl:+.2f}"
                ),
            )
            changed = True
            continue

        if campaign.status != "active" or pos.dca_paused:
            continue

        states = (
            db.query(PositionDcaState)
            .join(DcaRule, DcaRule.id == PositionDcaState.dca_rule_id)
            .filter(PositionDcaState.position_id == pos.id)
            .order_by(DcaRule.drop_pct.asc(), DcaRule.id.asc())
            .all()
        )
        for state in states:
            if state.executed:
                continue
            rule = state.rule
            drop_pct = float(state.custom_drop_pct if state.custom_drop_pct is not None else rule.drop_pct)
            alloc_pct = float(state.custom_allocation_pct if state.custom_allocation_pct is not None else rule.allocation_pct)
            support_score_raw = state.custom_support_score
            support_score = float(support_score_raw or 0.0)
            if campaign.ai_dca_enabled:
                if bool(campaign.strict_support_score_required) and support_score_raw is None:
                    continue
                if support_score and support_score < settings.dca_support_score_threshold:
                    continue
            trigger_price = pos.initial_price * (1 - (drop_pct / 100.0))
            if price > trigger_price:
                continue

            usdt = campaign.entry_amount_usdt * (alloc_pct / 100.0)
            if usdt <= 0 or get_usdt_free() < usdt:
                continue

            if campaign.trend_filter_enabled:
                if btc_state == "strong_bearish":
                    continue
                if btc_state == "bearish":
                    usdt *= 0.5

            if campaign.ai_dca_enabled:
                key = (pos.symbol, round(trigger_price, 6))
                if key not in ai_filter_cache:
                    ai_filter_cache[key] = _ai_dca_confirm(pos.symbol, trigger_price)
                allowed, breakdown_hit, _ = ai_filter_cache[key]
                if breakdown_hit:
                    pos.dca_paused = True
                    pos.dca_pause_reason = "strong_breakdown_detected"
                    changed = True
                    continue
                if not allowed:
                    continue
            try:
                buy = place_market_buy_quote(pos.symbol, usdt)
            except Exception as e:
                add_live_log(db, "LIVE_DCA_FAIL", pos.symbol, f"Campaign={campaign.name} | Rule={rule.name} | error={e}")
                continue
            qty = float(buy["executed_qty"])
            spent = float(buy["quote_qty"])
            if qty <= 0 or spent <= 0:
                continue
            avg = float(buy["avg_price"] or price)
            pos.total_invested_usdt += spent
            pos.total_qty += qty
            pos.average_price = pos.total_invested_usdt / pos.total_qty
            state.executed = True
            state.executed_at = now
            state.executed_price = avg
            state.executed_qty = qty
            state.executed_usdt = spent
            add_live_log(
                db,
                "LIVE_DCA",
                pos.symbol,
                (
                    f"Campaign={campaign.name} | Rule={rule.name} | Buy={avg:.6f} | Qty={qty:.8f} "
                    f"| USDT={spent:.2f} | Avg={pos.average_price:.6f}"
                ),
            )
            changed = True

    if changed:
        db.commit()


def recalculate_live_campaign_dca(db: Session, campaign: Campaign) -> tuple[int, int]:
    if campaign.mode != "live":
        return 0, 0
    rules = (
        db.query(DcaRule)
        .filter(DcaRule.campaign_id == campaign.id)
        .order_by(DcaRule.drop_pct.asc(), DcaRule.id.asc())
        .all()
    )
    if not rules:
        return 0, 0
    fallback_ai_rules = [(r.name, float(r.drop_pct), float(r.allocation_pct)) for r in rules]
    ai_profile = campaign.ai_dca_profile or "neutral"
    open_positions = db.query(Position).filter(Position.campaign_id == campaign.id, Position.status == "open").all()
    touched_positions = 0
    updated_states = 0
    for pos in open_positions:
        states = (
            db.query(PositionDcaState)
            .join(DcaRule, DcaRule.id == PositionDcaState.dca_rule_id)
            .filter(PositionDcaState.position_id == pos.id)
            .order_by(DcaRule.id.asc())
            .all()
        )
        if not states:
            continue
        if campaign.ai_dca_enabled:
            symbol_rules = build_symbol_ai_dca_rules(pos.symbol, ai_profile, fallback_ai_rules, campaign.sl_pct)
        else:
            symbol_rules = [(r.name, float(r.drop_pct), float(r.allocation_pct), None) for r in rules]
        symbol_rules_by_name = {name: (drop, alloc, score) for name, drop, alloc, score in symbol_rules}
        changed_any = False
        for st in states:
            if st.executed:
                continue
            rule_data = symbol_rules_by_name.get(st.rule.name)
            if not rule_data:
                st.custom_allocation_pct = 0.0
                st.custom_support_score = None
                changed_any = True
                updated_states += 1
                continue
            drop_pct, alloc_pct, support_score = rule_data
            st.custom_drop_pct = float(drop_pct)
            st.custom_allocation_pct = float(alloc_pct)
            st.custom_support_score = float(support_score) if support_score is not None else None
            changed_any = True
            updated_states += 1
        if changed_any:
            pos.dca_paused = False
            pos.dca_pause_reason = None
            touched_positions += 1
    return touched_positions, updated_states
