---
paths:
  - "**/*.py"
  - "**/*.pyi"
---
# Python Testing

## Framework

Use **pytest** as the testing framework. All tests go in `tests/`.

## Coverage Target

```bash
pytest --cov=app --cov-report=term-missing
# Target: 80%+ overall, 100% on order execution paths
```

## Test Organization

```
tests/
├── conftest.py          # Shared fixtures (mock DB, mock Binance)
├── unit/                # Fast tests, no DB, no real API
└── integration/         # Tests using in-memory SQLite DB
```

## Test Categorization

```python
import pytest

@pytest.mark.unit
def test_support_score_calculation():
    ...

@pytest.mark.integration
def test_campaign_creation_endpoint():
    ...

@pytest.mark.slow
def test_full_trading_cycle():
    ...
```

## Golden Rule

**Never call the real Binance API in tests.** Always mock it:

```python
from unittest.mock import patch

@patch("app.services.binance_live.requests.post")
def test_order_placement(mock_post):
    mock_post.return_value.json.return_value = {"orderId": "123", "status": "FILLED"}
    ...
```

## Reference

See skill: `python-testing` for detailed pytest patterns, fixtures, and mocking strategies.
