---
paths:
  - "**/*.py"
  - "**/*.pyi"
---
# Python Security

## Secret Management

```python
import os
from dotenv import load_dotenv

load_dotenv()

# Raises KeyError if missing — fail fast, don't silently use None
api_key = os.environ["BINANCE_API_KEY"]
api_secret = os.environ["BINANCE_API_SECRET"]
```

## Never Log Secrets

```python
# BAD
logger.debug(f"Using API key: {api_key}")
logger.debug(f"Response: {response.text}")  # May contain keys

# GOOD
logger.debug("API key loaded", extra={"key_length": len(api_key)})
logger.debug("Order response", extra={"order_id": response.get("orderId")})
```

## Security Scanning

Run regularly:

```bash
bandit -r app/          # Static security analysis
pip-audit               # Vulnerable dependency check
```

## Reference

See skill: `security-review` and `llm-trading-agent-security` for comprehensive trading bot security guidelines.
