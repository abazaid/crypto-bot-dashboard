from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text

from app.core.database import Base


class AITrade(Base):
    __tablename__ = "ai_trades"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), index=True, nullable=False)
    ai_provider = Column(String(20), index=True, nullable=False, default="openai")
    status = Column(String(20), index=True, nullable=False, default="open")
    entry_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    exit_time = Column(DateTime, nullable=True)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    quantity = Column(Float, nullable=False)
    notional_usdt = Column(Float, nullable=False, default=0.0)
    tp_price = Column(Float, nullable=False)
    sl_price = Column(Float, nullable=False)
    trailing_stop_price = Column(Float, nullable=True)
    trailing_active = Column(Integer, nullable=False, default=0)
    highest_price = Column(Float, nullable=True)
    pnl = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    exit_reason = Column(String(50), nullable=True)
    strategy_id = Column(String(120), index=True, nullable=False, default="-")
    strategy_json = Column(Text, nullable=False, default="{}")
