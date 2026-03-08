from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String

from app.core.database import Base


class AIProviderUsage(Base):
    __tablename__ = "ai_provider_usage"

    id = Column(Integer, primary_key=True, index=True)
    ai_provider = Column(String(20), index=True, nullable=False)
    call_type = Column(String(30), index=True, nullable=False, default="unknown")
    model_name = Column(String(80), nullable=True)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    estimated_cost_usd = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

