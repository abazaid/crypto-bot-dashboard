# Crypto Bots — Claude Code Guide

## Project Overview

Campaign-based crypto trading automation platform for live trading (real Binance + KuCoin execution). Built with Python 3, FastAPI, SQLAlchemy, and APScheduler.

## Stack

- **Backend**: Python 3, FastAPI, SQLAlchemy 2.0, APScheduler
- **Database**: SQLite (default), configurable
- **API**: Binance REST API
- **Server**: Uvicorn ASGI

## Key Files

| File | Purpose |
|------|---------|
| `app/main.py` | Router hub, endpoints, scheduler setup |
| `app/services/analytics.py` | Market analysis, support scoring, DCA planning |
| `app/services/live_trading.py` | Live Binance order execution |
| `app/services/binance_live.py` | Binance API wrapper |
| `app/models/trading.py` | SQLAlchemy ORM models |
| `app/core/config.py` | Settings from environment variables |
| `app/services/ai_pool_service.py` | AI Trader isolated pool (ledger, exits, breakers) — see `docs/ai_trader.md` |
| `app/services/ai_strategy.py` | AI Trader strategy ensemble (breakout / pullback / squeeze) |
| `app/services/telegram_signal_service.py` | Telegram signal follower pool — see `docs/telegram_signals.md` |

## Available Agents

Use these agents by asking Claude to invoke them:

| Agent | When to Use |
|-------|------------|
| `python-reviewer` | After writing or changing any `.py` file |
| `security-reviewer` | Before deploying live trading changes |
| `tdd-guide` | When adding new features or fixing bugs |
| `silent-failure-hunter` | When debugging or after changing services/ |
| `performance-optimizer` | When trading loop is slow |

## Available Skills

Claude will automatically use these skills when relevant:

| Skill | Coverage |
|-------|---------|
| `python-patterns` | Pythonic idioms, error handling, type hints |
| `python-testing` | pytest, fixtures, mocking Binance API |
| `security-review` | Input validation, secrets, SQL injection |
| `llm-trading-agent-security` | Spend limits, circuit breakers, key management |
| `database-migrations` | Alembic migrations for schema changes |

## Coding Standards

- **Never use bare `except:`** — always catch specific exceptions and log them
- **Use `logging` not `print()`** — all output via Python logging module
- **Type hints on all public functions** — enforced by `python-reviewer`
- **Mock Binance API in all tests** — never hit real API in tests
- **Secrets from env vars only** — never hardcoded

## Running the Project

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Running Tests

```bash
pytest --cov=app --cov-report=term-missing
pytest -m unit          # Fast unit tests only
pytest -m "not slow"    # Skip slow tests
```

## Known Issues to Fix (Priority Order)

1. **27 bare except blocks** — `grep -n "except:" app/ -r` shows them all
2. **Test suite is partial** — `tests/` covers the AI Trader pool (indicators, strategy, ledger); support scoring and legacy services still need tests
3. **No Alembic migrations** — schema changes are currently manual
4. **Unbounded ActivityLog/MarketSnapshot tables** — need cleanup job
5. **No retry logic on Binance API** — add exponential backoff
