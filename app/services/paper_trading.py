from datetime import datetime

from sqlalchemy.orm import Session

from app.models.paper_v2 import ActivityLog, AppSetting, Campaign, DcaRule, Position, PositionDcaState
from app.services.binance_public import get_prices


def get_setting(db: Session, key: str, default: str) -> str:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if not row:
        return default
    return row.value


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


def add_log(db: Session, event_type: str, symbol: str, message: str) -> None:
    db.add(ActivityLog(event_type=event_type, symbol=symbol or "-", message=message))


def ensure_defaults(db: Session, start_balance: float) -> None:
    if db.query(AppSetting).filter(AppSetting.key == "paper_cash").first() is None:
        set_setting(db, "paper_cash", f"{start_balance:.8f}")
        add_log(db, "SYSTEM", "-", f"Initialized paper wallet: {start_balance:.2f} USDT")
        db.commit()


def wallet_snapshot(db: Session) -> dict:
    cash = float(get_setting(db, "paper_cash", "0"))
    open_positions = db.query(Position).filter(Position.status == "open").all()
    symbols = sorted({p.symbol for p in open_positions})
    prices = get_prices(symbols) if symbols else {}
    invested_open = sum(float(p.total_invested_usdt) for p in open_positions)
    market_value = sum(float(prices.get(p.symbol, p.average_price)) * float(p.total_qty) for p in open_positions)
    unrealized = market_value - invested_open
    closed = db.query(Position).filter(Position.status == "closed").all()
    realized = sum(float(p.realized_pnl_usdt or 0.0) for p in closed)
    equity = cash + market_value
    return {
        "cash": cash,
        "invested_open": invested_open,
        "market_value": market_value,
        "unrealized_pnl": unrealized,
        "realized_pnl": realized,
        "equity": equity,
    }


def create_campaign_positions(db: Session, campaign: Campaign, symbols: list[str]) -> tuple[int, list[str]]:
    picked = sorted(set([s.strip().upper() for s in symbols if s and s.strip()]))
    if not picked:
        return 0, ["No symbols selected."]

    prices = get_prices(picked)
    valid = [s for s in picked if s in prices and prices[s] > 0]
    if not valid:
        return 0, ["No valid symbols with price feed."]

    wallet = wallet_snapshot(db)
    needed = campaign.entry_amount_usdt * len(valid)
    if wallet["cash"] < needed:
        return 0, [f"Insufficient paper cash. Need {needed:.2f} USDT, have {wallet['cash']:.2f} USDT."]

    rules = (
        db.query(DcaRule)
        .filter(DcaRule.campaign_id == campaign.id)
        .order_by(DcaRule.drop_pct.asc(), DcaRule.id.asc())
        .all()
    )

    opened = 0
    for symbol in valid:
        price = float(prices[symbol])
        qty = campaign.entry_amount_usdt / price
        pos = Position(
            campaign_id=campaign.id,
            symbol=symbol,
            initial_price=price,
            initial_qty=qty,
            total_invested_usdt=campaign.entry_amount_usdt,
            total_qty=qty,
            average_price=price,
        )
        db.add(pos)
        db.flush()
        for rule in rules:
            db.add(PositionDcaState(position_id=pos.id, dca_rule_id=rule.id, executed=False))
        add_log(
            db,
            "OPEN",
            symbol,
            (
                f"Campaign={campaign.name} | Initial buy at {price:.6f} "
                f"| Qty={qty:.8f} | USDT={campaign.entry_amount_usdt:.2f}"
            ),
        )
        opened += 1

    cash = wallet["cash"] - needed
    set_setting(db, "paper_cash", f"{cash:.8f}")
    db.commit()
    return opened, []


def run_cycle(db: Session) -> None:
    campaigns = db.query(Campaign).filter(Campaign.mode == "paper", Campaign.status == "active").all()
    if not campaigns:
        return

    open_positions = (
        db.query(Position)
        .join(Campaign, Campaign.id == Position.campaign_id)
        .filter(Position.status == "open", Campaign.status == "active", Campaign.mode == "paper")
        .all()
    )
    if not open_positions:
        return

    symbols = sorted({p.symbol for p in open_positions})
    prices = get_prices(symbols)
    if not prices:
        return

    cash = float(get_setting(db, "paper_cash", "0"))
    changed = False
    now = datetime.utcnow()

    for pos in open_positions:
        price = float(prices.get(pos.symbol, 0.0))
        if price <= 0:
            continue
        campaign = pos.campaign

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
            trigger_price = pos.initial_price * (1 - (float(rule.drop_pct) / 100.0))
            if price > trigger_price:
                continue

            usdt = campaign.entry_amount_usdt * (float(rule.allocation_pct) / 100.0)
            if usdt <= 0 or cash < usdt:
                continue
            qty = usdt / price
            pos.total_invested_usdt += usdt
            pos.total_qty += qty
            pos.average_price = pos.total_invested_usdt / pos.total_qty
            state.executed = True
            state.executed_at = now
            state.executed_price = price
            state.executed_qty = qty
            state.executed_usdt = usdt
            cash -= usdt
            changed = True
            add_log(
                db,
                "DCA",
                pos.symbol,
                (
                    f"Campaign={campaign.name} | Rule={rule.name} | Drop={rule.drop_pct:.2f}% "
                    f"| Buy at {price:.6f} | Qty={qty:.8f} | USDT={usdt:.2f} | Avg={pos.average_price:.6f}"
                ),
            )

        tp_hit = campaign.tp_pct is not None and price >= (pos.average_price * (1 + (campaign.tp_pct / 100.0)))
        sl_hit = campaign.sl_pct is not None and price <= (pos.average_price * (1 - (campaign.sl_pct / 100.0)))
        if not tp_hit and not sl_hit:
            continue

        proceeds = pos.total_qty * price
        pnl = proceeds - pos.total_invested_usdt
        pos.status = "closed"
        pos.closed_at = now
        pos.close_price = price
        pos.realized_pnl_usdt = pnl
        pos.close_reason = "TP" if tp_hit else "SL"
        cash += proceeds
        changed = True
        add_log(
            db,
            "CLOSE",
            pos.symbol,
            (
                f"Campaign={campaign.name} | Reason={pos.close_reason} | Close={price:.6f} "
                f"| Invested={pos.total_invested_usdt:.2f} | Proceeds={proceeds:.2f} | PnL={pnl:+.2f}"
            ),
        )

    if changed:
        set_setting(db, "paper_cash", f"{cash:.8f}")
        db.commit()
