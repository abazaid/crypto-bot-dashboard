"""
Live Smart Campaign — real Binance order execution driven by Advisor recommendations.
Completely separate from paper SmartCampaign for safety.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.database import Base


class LiveSmartCampaign(Base):
    __tablename__ = "live_smart_campaigns"

    id                = Column(Integer, primary_key=True)
    status            = Column(String, default="running", index=True)  # running | stopped
    max_symbols       = Column(Integer, default=3)
    entry_amount_usdt = Column(Float, default=50.0)
    created_at        = Column(DateTime, default=datetime.utcnow)
    updated_at        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    positions = relationship("LiveSmartPosition", back_populates="campaign", lazy="select")
    logs      = relationship("LiveSmartCampaignLog", back_populates="campaign", lazy="select")


class LiveSmartPosition(Base):
    __tablename__ = "live_smart_positions"

    id          = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("live_smart_campaigns.id"), index=True)
    symbol      = Column(String, index=True)

    # ── Entry ────────────────────────────────────────────────────────────
    entry_price        = Column(Float)
    entry_amount_usdt  = Column(Float)
    qty                = Column(Float)        # filled qty from Binance
    binance_order_id   = Column(String, nullable=True)

    # ── Advisor params captured at entry ─────────────────────────────────
    tp_pct      = Column(Float)
    sl_pct      = Column(Float)
    dca_drop_1  = Column(Float)
    dca_drop_2  = Column(Float)
    dca_alloc_1 = Column(Float)
    dca_alloc_2 = Column(Float)

    # ── DCA state ────────────────────────────────────────────────────────
    dca1_triggered    = Column(Boolean, default=False)
    dca1_price        = Column(Float, nullable=True)
    dca1_qty          = Column(Float, nullable=True)
    dca1_order_id     = Column(String, nullable=True)
    dca1_skipped      = Column(Boolean, default=False)   # skipped due to low balance
    dca2_triggered    = Column(Boolean, default=False)
    dca2_price        = Column(Float, nullable=True)
    dca2_qty          = Column(Float, nullable=True)
    dca2_order_id     = Column(String, nullable=True)
    dca2_skipped      = Column(Boolean, default=False)

    # ── Running totals ───────────────────────────────────────────────────
    avg_price           = Column(Float)
    total_invested_usdt = Column(Float)
    total_qty           = Column(Float)

    # ── Live state ───────────────────────────────────────────────────────
    current_price = Column(Float, nullable=True)
    pnl_pct       = Column(Float, nullable=True)
    pnl_usdt      = Column(Float, nullable=True)

    # ── Lifecycle ────────────────────────────────────────────────────────
    status         = Column(String, default="active", index=True)
    # active | sold_tp | sold_sl | sold_manual
    created_at     = Column(DateTime, default=datetime.utcnow)
    closed_at      = Column(DateTime, nullable=True)
    close_reason   = Column(String, nullable=True)
    close_price    = Column(Float, nullable=True)
    close_pnl_usdt = Column(Float, nullable=True)
    close_order_id = Column(String, nullable=True)

    campaign = relationship("LiveSmartCampaign", back_populates="positions")


class LiveSmartCampaignLog(Base):
    """Detailed activity log — every buy/sell/skip decision with full context."""
    __tablename__ = "live_smart_campaign_logs"

    id          = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("live_smart_campaigns.id"), nullable=True, index=True)
    position_id = Column(Integer, nullable=True)
    symbol      = Column(String, nullable=True, index=True)

    # Event type: OPEN | DCA1 | DCA2 | CLOSE_TP | CLOSE_SL | CLOSE_MANUAL |
    #             SKIP_BALANCE | SKIP_SIGNAL | SKIP_DCA_BALANCE | DCA_SKIPPED | ERROR
    event       = Column(String, index=True)
    message     = Column(Text)

    price          = Column(Float, nullable=True)
    amount_usdt    = Column(Float, nullable=True)
    pnl_usdt       = Column(Float, nullable=True)
    pnl_pct        = Column(Float, nullable=True)
    balance_before = Column(Float, nullable=True)
    balance_after  = Column(Float, nullable=True)

    created_at  = Column(DateTime, default=datetime.utcnow)

    campaign = relationship("LiveSmartCampaign", back_populates="logs")
