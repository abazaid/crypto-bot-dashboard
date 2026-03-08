from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String

from app.core.database import Base


class MarketObservation(Base):
    __tablename__ = "market_observations"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), index=True, nullable=False)
    observed_at = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)
    last_price = Column(Float, nullable=False, default=0.0)
    volume_24h = Column(Float, nullable=False, default=0.0)
    spread_pct = Column(Float, nullable=False, default=0.0)
    score = Column(Float, nullable=False, default=0.0)
    trend_status = Column(String(20), nullable=False, default="Neutral")
    signal_status = Column(String(30), nullable=False, default="No Data")
    decision_reason = Column(String(120), nullable=False, default="-")
