from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), default="paper", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    entry_amount_usdt: Mapped[float] = mapped_column(Float, nullable=False)
    tp_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_dca_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    smart_dca_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_dca_profile: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ai_dca_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_dca_suggested_rules_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    strict_support_score_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trend_filter_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_reentry_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    loop_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    loop_v2_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    loop_target_count: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    dca_rules: Mapped[list["DcaRule"]] = relationship("DcaRule", back_populates="campaign", cascade="all, delete-orphan")
    positions: Mapped[list["Position"]] = relationship("Position", back_populates="campaign", cascade="all, delete-orphan")


class DcaRule(Base):
    __tablename__ = "dca_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    drop_pct: Mapped[float] = mapped_column(Float, nullable=False)
    allocation_pct: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    campaign: Mapped["Campaign"] = relationship("Campaign", back_populates="dca_rules")
    states: Mapped[list["PositionDcaState"]] = relationship(
        "PositionDcaState", back_populates="rule", cascade="all, delete-orphan"
    )


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    initial_price: Mapped[float] = mapped_column(Float, nullable=False)
    initial_qty: Mapped[float] = mapped_column(Float, nullable=False)
    total_invested_usdt: Mapped[float] = mapped_column(Float, nullable=False)
    total_qty: Mapped[float] = mapped_column(Float, nullable=False)
    average_price: Mapped[float] = mapped_column(Float, nullable=False)
    open_fee_usdt: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    close_fee_usdt: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    realized_pnl_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tp_order_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp_order_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    dca_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dca_pause_reason: Mapped[str | None] = mapped_column(String(160), nullable=True)

    campaign: Mapped["Campaign"] = relationship("Campaign", back_populates="positions")
    dca_states: Mapped[list["PositionDcaState"]] = relationship(
        "PositionDcaState", back_populates="position", cascade="all, delete-orphan"
    )


class PositionDcaState(Base):
    __tablename__ = "position_dca_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), nullable=False, index=True)
    dca_rule_id: Mapped[int] = mapped_column(ForeignKey("dca_rules.id"), nullable=False, index=True)
    executed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    custom_drop_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    custom_allocation_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    custom_support_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    executed_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    executed_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    executed_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)

    position: Mapped["Position"] = relationship("Position", back_populates="dca_states")
    rule: Mapped["DcaRule"] = relationship("DcaRule", back_populates="states")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(String(120), nullable=False)


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    symbol: Mapped[str] = mapped_column(String(30), default="-", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class SmartRuntimeState(Base):
    __tablename__ = "smart_runtime_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), nullable=False, unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="paper")
    execution_zones_locked: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    recalc_recommended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recalc_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    market_state: Mapped[str | None] = mapped_column(String(30), nullable=True)
    ema200: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    dynamic_zones_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_medium_refresh_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_slow_recalc_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="paper")
    market_state: Mapped[str] = mapped_column(String(30), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    ema200: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
