from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.core.database import Base


class AIAgentMemory(Base):
    __tablename__ = "ai_agent_memories"

    id = Column(Integer, primary_key=True, index=True)
    ai_provider = Column(String(20), index=True, nullable=False)
    memory_type = Column(String(40), index=True, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

