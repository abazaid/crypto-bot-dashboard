"""
AI Trader Pool — an isolated sub-wallet inside a real exchange account.

The pool only ever spends its own cash (allocated capital + realized profit)
and only ever sells quantities it bought itself. It never touches other
holdings or open orders in the same account.

Standalone tables, isolated from the main trading models.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.database import Base


class AiPool(Base):
    __tablename__ = "ai_pools"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), default="AI Trader")
    account = Column(String(32), default="binance_1", index=True)  # binance_1 | binance_2 | kucoin_1
    status = Column(String(20), default="running", index=True)  # running | paused | halted
    halt_reason = Column(String(200), nullable=True)
    risk_profile = Column(String(20), default="balanced")  # conservative | balanced | aggressive

    # ── Ledger (all USDT) ────────────────────────────────────────────────
    allocated_usdt = Column(Float, default=0.0)  # sum of deposits minus withdrawals
    cash_usdt = Column(Float, default=0.0)  # spendable cash owned by the pool
    realized_pnl_usdt = Column(Float, default=0.0)  # net of fees
    fees_paid_usdt = Column(Float, default=0.0)
    peak_equity_usdt = Column(Float, default=0.0)
    day_key = Column(String(10), nullable=True)  # YYYY-MM-DD (UTC) for daily loss limit
    day_start_equity_usdt = Column(Float, default=0.0)

    # ── Risk parameters (copied from profile at creation, editable) ─────
    risk_per_trade_pct = Column(Float, default=1.5)
    max_position_pct = Column(Float, default=35.0)
    max_positions = Column(Integer, default=4)
    min_entry_score = Column(Float, default=65.0)
    daily_loss_limit_pct = Column(Float, default=4.0)
    max_drawdown_pct = Column(Float, default=15.0)
    symbol_cooldown_hours = Column(Float, default=12.0)
    max_entries_per_hour = Column(Integer, default=2)
    time_stop_hours = Column(Float, default=72.0)
    avoid_account_holdings = Column(Boolean, default=True)
    max_portfolio_risk_pct = Column(Float, default=4.0)  # sum of open stop-distances, % of equity
    breaker_cooldown_hours = Column(Float, default=12.0)  # wait after a daily-loss breaker before re-entering
    breaker_at = Column(DateTime, nullable=True)
    profit_giveback_pct = Column(Float, default=50.0)  # never give back more than this % of peak open profit
    breakeven_at_r = Column(Float, default=0.6)  # move stop to breakeven (and arm the give-back guard) at this many R
    tp1_r = Column(Float, default=1.2)  # first partial take-profit at this many R
    tp1_fraction = Column(Float, default=0.4)  # fraction sold at TP1

    # ── Stats ────────────────────────────────────────────────────────────
    trades_won = Column(Integer, default=0)
    trades_lost = Column(Integer, default=0)
    consecutive_errors = Column(Integer, default=0)
    last_scan_at = Column(DateTime, nullable=True)
    last_tick_at = Column(DateTime, nullable=True)
    market_state = Column(String(20), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    positions = relationship("AiPoolPosition", back_populates="pool", lazy="select")


class AiPoolPosition(Base):
    __tablename__ = "ai_pool_positions"

    id = Column(Integer, primary_key=True)
    pool_id = Column(Integer, ForeignKey("ai_pools.id"), index=True)
    symbol = Column(String(24), index=True)
    strategy = Column(String(32))  # breakout | pullback | squeeze
    status = Column(String(20), default="open", index=True)  # open | closed

    # ── Ownership (the only qty the pool is allowed to sell) ────────────
    qty = Column(Float, default=0.0)  # net qty currently held by the pool
    qty_initial = Column(Float, default=0.0)
    avg_entry = Column(Float, default=0.0)
    invested_usdt = Column(Float, default=0.0)  # cost basis of remaining qty
    entry_fee_usdt = Column(Float, default=0.0)

    # ── Risk plan ────────────────────────────────────────────────────────
    stop_price = Column(Float, default=0.0)
    initial_stop_price = Column(Float, default=0.0)
    tp1_price = Column(Float, default=0.0)
    tp1_done = Column(Boolean, default=False)
    trail_atr = Column(Float, default=0.0)  # ATR value at entry (4h)
    trail_mult = Column(Float, default=2.5)
    highest_price = Column(Float, default=0.0)
    add_on_done = Column(Boolean, default=False)
    entry_score = Column(Float, default=0.0)
    entry_reason = Column(Text, nullable=True)

    # ── Live ─────────────────────────────────────────────────────────────
    current_price = Column(Float, nullable=True)
    unrealized_pnl_usdt = Column(Float, default=0.0)
    unrealized_pnl_pct = Column(Float, default=0.0)
    realized_pnl_usdt = Column(Float, default=0.0)  # from partial exits
    external_flag = Column(String(120), nullable=True)

    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)
    close_reason = Column(String(40), nullable=True)
    # stop | tp_trail | time_stop | manual | regime | external | close_all

    pool = relationship("AiPool", back_populates="positions")


class AiPoolTrade(Base):
    """Every fill the pool executed (buys and sells, partial or full)."""
    __tablename__ = "ai_pool_trades"

    id = Column(Integer, primary_key=True)
    pool_id = Column(Integer, ForeignKey("ai_pools.id"), index=True)
    position_id = Column(Integer, index=True, nullable=True)
    symbol = Column(String(24), index=True)
    side = Column(String(4))  # BUY | SELL
    kind = Column(String(24))  # entry | add_on | tp1 | trail | stop | time_stop | manual | close_all | regime
    qty = Column(Float, default=0.0)
    price = Column(Float, default=0.0)
    quote_usdt = Column(Float, default=0.0)
    fee_usdt = Column(Float, default=0.0)
    pnl_usdt = Column(Float, nullable=True)  # for sells, net of fees
    order_id = Column(String(40), nullable=True)
    client_order_id = Column(String(40), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class AiPoolLog(Base):
    """Decision log — every entry, exit, skip, breaker, and error with the reason."""
    __tablename__ = "ai_pool_logs"

    id = Column(Integer, primary_key=True)
    pool_id = Column(Integer, ForeignKey("ai_pools.id"), index=True, nullable=True)
    symbol = Column(String(24), nullable=True, index=True)
    event = Column(String(32), index=True)
    message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
