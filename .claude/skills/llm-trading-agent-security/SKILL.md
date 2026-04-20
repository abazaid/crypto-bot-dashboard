---
name: llm-trading-agent-security
description: Security patterns for autonomous trading agents with wallet or transaction authority. Covers prompt injection, spend limits, pre-send simulation, circuit breakers, MEV protection, and key handling.
origin: ECC direct-port adaptation
version: "1.0.0"
---

# LLM Trading Agent Security

Autonomous trading agents have a harsher threat model than normal LLM apps: an injection or bad tool path can turn directly into asset loss.

## When to Use

- Building an AI agent that places real orders on Binance
- Auditing the live trading service for safety issues
- Designing API key management for the bot
- Adding new features that touch order execution

## How It Works

Layer the defenses. No single check is enough. Treat prompt hygiene, spend policy, simulation, execution limits, and wallet isolation as independent controls.

## Examples

### Hard spend limits

```python
from decimal import Decimal

MAX_SINGLE_ORDER_USD = Decimal("500")
MAX_DAILY_SPEND_USD = Decimal("2000")

class SpendLimitError(Exception):
    pass

class SpendLimitGuard:
    def check_and_record(self, usd_amount: Decimal) -> None:
        if usd_amount > MAX_SINGLE_ORDER_USD:
            raise SpendLimitError(
                f"Single order ${usd_amount} exceeds max ${MAX_SINGLE_ORDER_USD}"
            )

        daily = self._get_24h_spend()
        if daily + usd_amount > MAX_DAILY_SPEND_USD:
            raise SpendLimitError(
                f"Daily limit: ${daily} + ${usd_amount} > ${MAX_DAILY_SPEND_USD}"
            )

        self._record_spend(usd_amount)
```

### Circuit breaker

```python
class TradingCircuitBreaker:
    MAX_CONSECUTIVE_LOSSES = 3
    MAX_HOURLY_LOSS_PCT = 0.05

    def __init__(self):
        self.consecutive_losses = 0
        self.hour_start_value = 0.0
        self.halted = False

    def check(self, portfolio_value: float) -> None:
        if self.halted:
            raise RuntimeError("Trading halted by circuit breaker")

        if self.consecutive_losses >= self.MAX_CONSECUTIVE_LOSSES:
            self.halt("Too many consecutive losses")

        if self.hour_start_value > 0:
            hourly_pnl = (portfolio_value - self.hour_start_value) / self.hour_start_value
            if hourly_pnl < -self.MAX_HOURLY_LOSS_PCT:
                self.halt(f"Hourly PnL {hourly_pnl:.1%} below threshold")

    def halt(self, reason: str) -> None:
        self.halted = True
        import logging
        logging.critical(f"CIRCUIT BREAKER TRIGGERED: {reason}")
        # TODO: Send alert (email/Telegram)
        raise RuntimeError(f"Circuit breaker: {reason}")
```

### API key isolation

```python
import os

# GOOD: Keys from environment only
api_key = os.environ.get("BINANCE_API_KEY")
api_secret = os.environ.get("BINANCE_API_SECRET")

if not api_key or not api_secret:
    raise EnvironmentError("BINANCE_API_KEY and BINANCE_API_SECRET must be set")

# GOOD: Use a dedicated trading API key with only TRADE permission
# NEVER use a key with WITHDRAWAL permission on the bot
```

### Retry with limits

```python
import time
import logging

def binance_request_with_retry(fn, max_retries: int = 3):
    """Execute a Binance API call with limited retries."""
    for attempt in range(max_retries):
        try:
            return fn()
        except (ConnectionError, TimeoutError) as e:
            if attempt == max_retries - 1:
                logging.error(
                    f"Binance API failed after {max_retries} attempts: {e}"
                )
                raise
            wait = 2 ** attempt  # 1s, 2s, 4s
            logging.warning(f"Binance API attempt {attempt+1} failed, retrying in {wait}s")
            time.sleep(wait)
```

### Logging without leaking keys

```python
import logging
import re

def sanitize_log_message(message: str) -> str:
    """Remove potential API keys from log messages."""
    # Binance API keys are 64 hex chars
    return re.sub(r'[0-9a-fA-F]{64}', '[REDACTED]', message)

# Use structured logging with context, not raw API responses
logging.info(
    "Order placed",
    extra={
        "campaign_id": campaign.id,
        "symbol": symbol,
        "side": "BUY",
        "amount_usd": amount,
        "order_id": order_id,  # OK to log
        # NEVER log: api_key, api_secret, full response body
    }
)
```

## Pre-Deploy Checklist

- [ ] API keys come from env vars, never hardcoded
- [ ] Binance API key has TRADE-only permission (no WITHDRAWAL)
- [ ] Spend limits enforced independently of campaign logic
- [ ] Circuit breakers configured for consecutive losses
- [ ] All order attempts audit-logged (success AND failure)
- [ ] Retry logic has a maximum attempt cap
- [ ] Logs sanitized — no API keys or secrets in log output
- [ ] Paper mode tested thoroughly before enabling live mode
- [ ] Separate API keys for paper test account vs live account
