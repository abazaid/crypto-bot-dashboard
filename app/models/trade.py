from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String

from app.core.database import Base


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), index=True, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    quantity = Column(Float, nullable=False)
    status = Column(String(20), index=True, nullable=False, default="open")
    tp_price = Column(Float, nullable=False)
    sl_price = Column(Float, nullable=False)
    entry_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    exit_time = Column(DateTime, nullable=True)
    pnl = Column(Float, nullable=True)
    exit_reason = Column(String(50), nullable=True)
    highest_price = Column(Float, nullable=True)
    trailing_active = Column(Integer, nullable=False, default=0)
    trailing_stop_price = Column(Float, nullable=True)
