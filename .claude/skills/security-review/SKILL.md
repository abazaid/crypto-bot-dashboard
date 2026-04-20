---
name: security-review
description: Security checklist and patterns for Python FastAPI applications. Use when adding authentication, handling user input, working with secrets, creating API endpoints, or implementing trading features.
origin: ECC
---

# Security Review Skill

This skill ensures all code follows security best practices for the crypto-bots trading platform.

## When to Activate

- Adding new API endpoints to main.py
- Handling user input (campaign creation, DCA rule configuration)
- Working with Binance API keys or secrets
- Implementing new order execution logic
- Adding new environment variables

## Security Checklist

### 1. Secrets Management

```python
# BAD: Hardcoded credentials
BINANCE_API_KEY = "abc123..."
BINANCE_SECRET = "xyz456..."

# GOOD: From environment
import os
api_key = os.environ.get("BINANCE_API_KEY")
api_secret = os.environ.get("BINANCE_API_SECRET")

if not api_key or not api_secret:
    raise EnvironmentError("Binance credentials not configured")
```

Verification:
- [ ] No hardcoded API keys, tokens, or passwords
- [ ] All secrets in environment variables or `.env` file
- [ ] `.env` in `.gitignore`
- [ ] No secrets in git history

### 2. Input Validation

```python
from pydantic import BaseModel, validator
from fastapi import HTTPException

class CampaignCreate(BaseModel):
    name: str
    symbol: str
    entry_amount: float
    tp_pct: Optional[float] = None
    sl_pct: Optional[float] = None

    @validator("symbol")
    def symbol_must_end_with_usdt(cls, v):
        if not v.endswith("USDT"):
            raise ValueError("Only USDT pairs supported")
        return v.upper()

    @validator("entry_amount")
    def entry_amount_must_be_positive(cls, v):
        if v <= 0:
            raise ValueError("Entry amount must be positive")
        return v

    @validator("sl_pct")
    def sl_must_be_negative_or_none(cls, v):
        if v is not None and v >= 0:
            raise ValueError("Stop loss must be negative percentage")
        return v
```

Verification:
- [ ] All user inputs validated with Pydantic models
- [ ] Symbol format validated before Binance API calls
- [ ] Numeric values have bounds checking (no negative amounts, no 1000% TP)
- [ ] Error messages don't leak internal details

### 3. Error Handling — No Silent Failures

```python
# BAD: Silent failure — order may be lost
try:
    order = place_order(symbol, amount)
except Exception:
    pass

# GOOD: Log everything, re-raise or handle explicitly
try:
    order = place_order(symbol, amount)
except BinanceAPIError as e:
    logger.error(
        f"Order placement failed",
        extra={"symbol": symbol, "amount": amount, "error": str(e)}
    )
    raise  # Don't swallow — caller must know
except Exception as e:
    logger.critical(
        f"Unexpected error placing order",
        extra={"symbol": symbol, "error": str(e)},
        exc_info=True
    )
    raise
```

### 4. Logging — No Sensitive Data

```python
# BAD: Logging API keys or full responses
logger.debug(f"Binance response: {response.text}")
logger.info(f"Using key: {api_key}")

# GOOD: Log only relevant identifiers
logger.info(
    "Order placed successfully",
    extra={
        "order_id": response["orderId"],
        "symbol": symbol,
        "campaign_id": campaign_id,
        # NOT: api_key, api_secret, full response body
    }
)
```

### 5. SQL Injection Prevention (SQLAlchemy)

```python
# BAD: Raw string in query
symbol = request.form.get("symbol")
db.execute(f"SELECT * FROM positions WHERE symbol = '{symbol}'")

# GOOD: Parameterized via SQLAlchemy ORM
positions = db.query(Position).filter(Position.symbol == symbol).all()

# GOOD: Raw SQL with parameters
db.execute(text("SELECT * FROM positions WHERE symbol = :symbol"), {"symbol": symbol})
```

### 6. Binance API Key Permissions

Ensure the Binance API key used by the bot has:
- ✅ Enable Reading
- ✅ Enable Spot & Margin Trading
- ❌ Enable Withdrawals (NEVER enable for bot)
- ❌ Enable Futures (unless needed)
- ✅ IP restriction (restrict to your server IP)

### 7. Rate Limiting on Endpoints

```python
from fastapi import Request
from collections import defaultdict
import time

request_counts = defaultdict(list)

def check_rate_limit(ip: str, max_requests: int = 30, window_seconds: int = 60):
    now = time.time()
    request_counts[ip] = [t for t in request_counts[ip] if now - t < window_seconds]
    if len(request_counts[ip]) >= max_requests:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    request_counts[ip].append(now)
```

## Pre-Deployment Security Checklist

Before ANY production deployment:

- [ ] **Secrets**: No hardcoded secrets, all in env vars
- [ ] **Input Validation**: All campaign/position inputs validated with Pydantic
- [ ] **SQL Injection**: All queries via SQLAlchemy ORM (no raw f-string SQL)
- [ ] **Error Handling**: No bare excepts, all errors logged with context
- [ ] **Logging**: No API keys, secrets, or sensitive data in logs
- [ ] **Binance API Key**: TRADE permission only, no WITHDRAWAL, IP-restricted
- [ ] **Spend Limits**: Hard limits on single order size and daily spend
- [ ] **Circuit Breakers**: Configured for consecutive losses and hourly drawdown
- [ ] **Dependencies**: `pip-audit` shows no known vulnerabilities
- [ ] **CORS**: Configured if exposing API externally

---

**Remember**: A security bug in a trading bot causes direct financial loss. When in doubt, err on the side of caution.
