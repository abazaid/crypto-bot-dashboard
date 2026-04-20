---
paths:
  - "**/*.py"
  - "**/*.pyi"
---
# Python Coding Style

## Standards

- Follow **PEP 8** conventions
- Use **type annotations** on all function signatures
- Use **logging** module — never `print()` for anything beyond scripts

## Naming Conventions

- Functions and variables: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`
- Private methods: `_leading_underscore`

## Error Handling

**Never use bare except:**

```python
# BAD — blocks all exceptions including KeyboardInterrupt, SystemExit
try:
    risky()
except:
    pass

# GOOD — catch specific, log with context
try:
    risky()
except BinanceAPIError as e:
    logger.error(f"Binance API failed for {symbol}: {e}")
    raise
```

## Immutability

Prefer immutable data structures for config and DTOs:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class CampaignSettings:
    symbol: str
    mode: str
    entry_amount: float
```

## Formatting Tools

- **black** for code formatting
- **isort** for import sorting
- **ruff** for linting

## Reference

See skill: `python-patterns` for comprehensive Python idioms and patterns.
