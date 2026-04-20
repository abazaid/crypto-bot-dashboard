---
paths:
  - "**/*.py"
  - "**/*.pyi"
---
# Python Patterns

## Protocol (Duck Typing)

Use Protocol for abstractions between paper and live trading:

```python
from typing import Protocol

class TradingService(Protocol):
    def place_order(self, symbol: str, amount: float) -> dict: ...
    def get_price(self, symbol: str) -> float: ...
    def cancel_order(self, order_id: str) -> bool: ...
```

## Dataclasses as DTOs

Use dataclasses for passing data between services:

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class OrderResult:
    order_id: str
    symbol: str
    filled_qty: float
    avg_price: float
    status: str
    error: Optional[str] = None
```

## Context Managers for DB Sessions

```python
from contextlib import contextmanager

@contextmanager
def get_db_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

## Generators for Large Datasets

```python
# Good: Stream activity logs without loading all in memory
def iter_recent_logs(db, days: int = 30):
    cutoff = datetime.utcnow() - timedelta(days=days)
    yield from db.query(ActivityLog).filter(
        ActivityLog.created_at > cutoff
    ).yield_per(100)
```

## Reference

See skill: `python-patterns` for comprehensive patterns including decorators, concurrency, and package organization.
