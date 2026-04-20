---
name: python-patterns
description: Pythonic idioms, PEP 8 standards, type hints, and best practices for building robust, efficient, and maintainable Python applications.
origin: ECC
---

# Python Development Patterns

Idiomatic Python patterns and best practices for building robust, efficient, and maintainable applications.

## When to Activate

- Writing new Python code
- Reviewing Python code
- Refactoring existing Python code
- Designing Python packages/modules

## Core Principles

### 1. Readability Counts

Python prioritizes readability. Code should be obvious and easy to understand.

```python
# Good: Clear and readable
def get_active_users(users: list[User]) -> list[User]:
    """Return only active users from the provided list."""
    return [user for user in users if user.is_active]


# Bad: Clever but confusing
def get_active_users(u):
    return [x for x in u if x.a]
```

### 2. Explicit is Better Than Implicit

Avoid magic; be clear about what your code does.

```python
# Good: Explicit configuration
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
```

### 3. EAFP - Easier to Ask Forgiveness Than Permission

Python prefers exception handling over checking conditions.

```python
# Good: EAFP style
def get_value(dictionary: dict, key: str) -> Any:
    try:
        return dictionary[key]
    except KeyError:
        return default_value
```

## Error Handling Patterns

### Specific Exception Handling

```python
# Good: Catch specific exceptions
def load_config(path: str) -> Config:
    try:
        with open(path) as f:
            return Config.from_json(f.read())
    except FileNotFoundError as e:
        raise ConfigError(f"Config file not found: {path}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in config: {path}") from e

# Bad: Bare except
def load_config(path: str) -> Config:
    try:
        with open(path) as f:
            return Config.from_json(f.read())
    except:
        return None  # Silent failure!
```

### Custom Exception Hierarchy

```python
class AppError(Exception):
    """Base exception for all application errors."""
    pass

class TradingError(AppError):
    """Raised when a trading operation fails."""
    pass

class BinanceAPIError(TradingError):
    """Raised when Binance API returns an error."""
    pass
```

## Type Hints

```python
from typing import Optional, List, Dict, Any

def process_campaign(
    campaign_id: int,
    symbol: str,
    active: bool = True
) -> Optional[dict]:
    """Process a campaign and return result or None."""
    if not active:
        return None
    return {"campaign_id": campaign_id, "symbol": symbol}
```

## Context Managers

```python
# Good: Using context managers
def process_file(path: str) -> str:
    with open(path, 'r') as f:
        return f.read()

# Custom context manager
from contextlib import contextmanager

@contextmanager
def db_transaction(session):
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
```

## Data Classes

```python
from dataclasses import dataclass, field

@dataclass
class CampaignConfig:
    """Campaign configuration DTO."""
    symbol: str
    entry_price: float
    tp_pct: Optional[float] = None
    sl_pct: Optional[float] = None
    dca_levels: list = field(default_factory=list)
```

## Concurrency Patterns

```python
import threading
import concurrent.futures

# Threading for I/O-bound tasks (Binance API calls)
def fetch_all_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch multiple prices concurrently."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_symbol = {executor.submit(fetch_price, s): s for s in symbols}
        results = {}
        for future in concurrent.futures.as_completed(future_to_symbol):
            symbol = future_to_symbol[future]
            try:
                results[symbol] = future.result()
            except Exception as e:
                logger.error(f"Price fetch failed for {symbol}: {e}")
    return results
```

## Anti-Patterns to Avoid

```python
# Bad: Mutable default arguments
def append_to(item, items=[]):
    items.append(item)
    return items

# Good: Use None
def append_to(item, items=None):
    if items is None:
        items = []
    items.append(item)
    return items

# Bad: Bare except
try:
    risky_operation()
except:
    pass

# Good: Specific exception
try:
    risky_operation()
except SpecificError as e:
    logger.error(f"Operation failed: {e}")

# Bad: print() for debugging
print(f"Order placed: {order_id}")

# Good: structured logging
logger.info("Order placed", extra={"order_id": order_id, "symbol": symbol})
```

## Tooling

```bash
black .          # Code formatting
isort .          # Import sorting
ruff check .     # Fast linting
mypy .           # Type checking
bandit -r .      # Security scanning
pytest --cov=app # Test coverage
```

__Remember__: Python code should be readable, explicit, and follow the principle of least surprise.
