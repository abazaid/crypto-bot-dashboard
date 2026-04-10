"""
Smart Campaign — auto-managed positions driven by Advisor recommendations.
Completely independent from the existing Campaign/Position models.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.core.database import Base


class SmartCampaign(Base):
    __tablename__ = "smart_campaigns"

    id              = Column(Integer, primary_key=True)
    status          = Column(String, default="running", index=True)  # running | stopped
    max_symbols     = Column(Integer, default=5)
    entry_amount_usdt = Column(Float, default=100.0)
    feature_version = Column(String, default="v1")      # v1 | v2
    mode            = Column(String, default="paper")   # paper (future: live)
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    positions = relationship(
        "SmartPosition", back_populates="campaign", lazy="select"
    )


class SmartPosition(Base):
    __tablename__ = "smart_positions"

    id          = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("smart_campaigns.id"), index=True)
    symbol      = Column(String, index=True)

    # ── Entry ────────────────────────────────────────────────────────────
    entry_price        = Column(Float)
    entry_amount_usdt  = Column(Float)
    qty                = Column(Float)

    # ── Advisor params captured at entry time ────────────────────────────
    tp_pct      = Column(Float)
    sl_pct      = Column(Float)
    dca_drop_1  = Column(Float)
    dca_drop_2  = Column(Float)
    dca_alloc_1 = Column(Float)
    dca_alloc_2 = Column(Float)

    # ── DCA state ────────────────────────────────────────────────────────
    dca1_triggered = Column(Boolean, default=False)
    dca1_price     = Column(Float, nullable=True)
    dca1_qty       = Column(Float, nullable=True)
    dca2_triggered = Column(Boolean, default=False)
    dca2_price     = Column(Float, nullable=True)
    dca2_qty       = Column(Float, nullable=True)

    # ── Running totals ───────────────────────────────────────────────────
    avg_price          = Column(Float)
    total_invested_usdt = Column(Float)
    total_qty          = Column(Float)

    # ── Live state ───────────────────────────────────────────────────────
    current_price = Column(Float, nullable=True)
    pnl_pct       = Column(Float, nullable=True)
    pnl_usdt      = Column(Float, nullable=True)

    # ── Lifecycle ────────────────────────────────────────────────────────
    status        = Column(String, default="active", index=True)
    # active | sold_tp | sold_sl | sold_manual | stopped
    created_at    = Column(DateTime, default=datetime.utcnow)
    closed_at     = Column(DateTime, nullable=True)
    close_reason  = Column(String, nullable=True)
    close_pnl_usdt = Column(Float, nullable=True)

    campaign = relationship("SmartCampaign", back_populates="positions")
