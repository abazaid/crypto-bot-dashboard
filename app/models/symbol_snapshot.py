from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String

from app.core.database import Base


class SymbolSnapshot(Base):
    __tablename__ = "symbols"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), index=True, nullable=False)
    volume_24h = Column(Float, nullable=False)
    spread_pct = Column(Float, nullable=False)
    last_price = Column(Float, nullable=False)
    trend_status = Column(String(20), nullable=False)
    signal_status = Column(String(20), nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
