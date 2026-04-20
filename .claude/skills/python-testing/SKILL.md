---
name: python-testing
description: Python testing strategies using pytest, TDD methodology, fixtures, mocking, parametrization, and coverage requirements. Tailored for FastAPI + SQLAlchemy + Binance API projects.
origin: ECC
---

# Python Testing Patterns

Comprehensive testing strategies for the crypto-bots FastAPI application using pytest.

## When to Activate

- Writing new trading logic (follow TDD: red, green, refactor)
- Adding new API endpoints
- Fixing bugs (write test that reproduces the bug first)
- Reviewing test coverage

## Core Testing Philosophy

Always follow the TDD cycle:

1. **RED**: Write a failing test for the desired behavior
2. **GREEN**: Write minimal code to make the test pass
3. **REFACTOR**: Improve code while keeping tests green

## Coverage Requirements

- **Target**: 80%+ code coverage
- **Critical paths**: 100% coverage (order execution, SL/TP logic)

```bash
pytest --cov=app --cov-report=term-missing --cov-report=html
```

## Project-Specific Fixtures

```python
# tests/conftest.py
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.core.database import Base, get_db

# Use in-memory SQLite for tests
TEST_DATABASE_URL = "sqlite:///:memory:"

@pytest.fixture(scope="session")
def engine():
    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)

@pytest.fixture
def db_session(engine):
    """Provides a transactional session that rolls back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session()
    yield session
    session.close()
    transaction.rollback()
    connection.close()

@pytest.fixture
def client(db_session):
    """FastAPI test client with overridden DB session."""
    def override_get_db():
        yield db_session
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()

@pytest.fixture
def mock_binance():
    """Mock Binance API to prevent real API calls in tests."""
    with patch("app.services.binance_live.requests.get") as mock_get, \
         patch("app.services.binance_live.requests.post") as mock_post:
        mock_get.return_value.json.return_value = {"price": "50000.00"}
        mock_get.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "orderId": "12345",
            "status": "FILLED",
            "executedQty": "0.001",
            "cummulativeQuoteQty": "50.00"
        }
        mock_post.return_value.status_code = 200
        yield {"get": mock_get, "post": mock_post}
```

## Testing Trading Logic

```python
# tests/unit/test_paper_trading.py
import pytest
from app.services.paper_trading import PaperTradingService

class TestSupportScoring:
    """Test support zone scoring logic."""

    @pytest.mark.parametrize("score,expected_action", [
        (0.0, "skip"),      # No score = no DCA
        (0.5, "normal"),    # Medium score = normal DCA
        (1.0, "strong"),    # High score = strong DCA
    ])
    def test_dca_action_by_score(self, score, expected_action):
        result = PaperTradingService.get_dca_action(score)
        assert result == expected_action

    def test_no_dca_without_support_score_in_strict_mode(self):
        """Strict mode: score=0 must skip DCA."""
        result = PaperTradingService.should_execute_dca(
            score=0.0, strict_mode=True
        )
        assert result is False

class TestDCAAllocation:
    """Test DCA capital allocation calculations."""

    def test_allocation_sums_to_100_pct(self):
        levels = [
            {"drop_pct": 5, "allocation_pct": 25},
            {"drop_pct": 10, "allocation_pct": 25},
            {"drop_pct": 20, "allocation_pct": 50},
        ]
        total = sum(l["allocation_pct"] for l in levels)
        assert total == 100

    def test_max_allocation_cap_respected(self):
        """Position size should never exceed MAX_SYMBOL_ALLOCATION_X."""
        result = PaperTradingService.calculate_dca_size(
            current_value=700.0,
            max_multiplier=7.0,
            initial_investment=100.0
        )
        assert result == 0.0  # At cap, no more DCA
```

## Testing FastAPI Endpoints

```python
# tests/integration/test_api_campaigns.py
import pytest

class TestCampaignAPI:

    def test_create_campaign(self, client, db_session):
        response = client.post("/campaigns/create", data={
            "name": "Test Campaign",
            "symbol": "BTCUSDT",
            "mode": "paper",
            "entry_amount": "100",
        })
        assert response.status_code == 200

    def test_campaign_list_returns_only_active(self, client, db_session):
        response = client.get("/campaigns/")
        assert response.status_code == 200

    def test_invalid_symbol_rejected(self, client):
        response = client.post("/campaigns/create", data={
            "symbol": "INVALID123",
        })
        assert response.status_code in (400, 422)
```

## Testing Error Paths

```python
# Always test what happens when things go WRONG
def test_binance_api_failure_handled_gracefully(mock_binance, db_session):
    """When Binance API fails, order should not be marked as filled."""
    mock_binance["post"].side_effect = ConnectionError("Network error")

    with pytest.raises(ConnectionError):
        place_live_order(symbol="BTCUSDT", amount=100.0)

    # Verify no order was recorded in DB
    orders = db_session.query(Order).filter_by(symbol="BTCUSDT").all()
    assert len(orders) == 0
```

## Markers

```python
# pytest.ini
[pytest]
markers =
    unit: Fast unit tests (no DB, no external calls)
    integration: Tests that use the DB
    slow: Tests that are slow (kline analysis, large datasets)
```

```bash
pytest -m "not slow"          # Skip slow tests in CI
pytest -m "unit"              # Only unit tests
pytest --cov=app -v           # Full run with coverage
```

**Remember**: In a trading bot, untested code is a financial liability. Test every edge case.
