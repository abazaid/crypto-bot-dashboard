# ADVANCED CRYPTO TRADING BOT – FINAL PROJECT BRIEF

## 1. Project Overview

Build a **production-ready automated trading system** for **Binance Spot** using Python.

The system must include:

• Automated trading engine
• Market scanner
• Risk management system
• Paper trading simulator
• Web dashboard
• Logging and monitoring
• Telegram alerts

The system must run on **Render.com without requiring a VPS**.

Default trading mode must be **Paper Trading**.

Live trading must only be enabled manually.

---

# 2. Primary Goals

The bot must:

Scan Binance markets
Find high liquidity coins
Apply a strategy targeting **~2% profit per trade**
Limit downside risk
Provide real-time monitoring through a dashboard.

---

# 3. Technology Stack

Backend

Python 3.11+

Framework

FastAPI

Database

SQLite (local)

PostgreSQL (Render)

Libraries

python-binance
pandas
numpy
ta
requests
SQLAlchemy
apscheduler

Frontend

Jinja templates
HTMX (optional)

Deployment

Render Web Service
Render Cron Job

---

# 4. Trading Philosophy

This is **NOT a scalping bot**.

Trades target **2–4% price moves**.

Execution interval:

Every **5 minutes**.

Strategy type:

**Trend Pullback Strategy**

---

# 5. Market Scanner

The bot scans all **USDT pairs** on Binance.

Filtering conditions:

24h Volume above configurable threshold

Spread below configurable threshold

Exclude extreme pumps

Exclude illiquid coins

Select **top N coins**

Example candidates:

BTCUSDT
ETHUSDT
SOLUSDT
LINKUSDT
AVAXUSDT

---

# 6. Market Protection Filter

Crypto markets are strongly correlated with BTC.

Rule:

If BTC trend is bearish → block new trades.

Trend detection:

BTC 1H chart

Condition:

EMA50 < EMA200

Then:

No new trades allowed.

Existing trades remain active.

---

# 7. Entry Strategy

Strategy: **Trend Pullback**

Trend timeframe

15 minutes

Entry timeframe

5 minutes

Trend rule

EMA50 > EMA200

Entry conditions

Price retraces to EMA20 or EMA50

RSI between 40 and 55

Volume spike relative to average

Signal generation

BUY signal when all conditions match.

---

# 8. Exit Strategy

Take Profit

2%

Stop Loss

1.2%

Optional Trailing Stop

Disabled by default.

Time Stop

Close trade if inactive for configurable duration.

Example

60–120 minutes.

---

# 9. Risk Management System

Critical protection rules.

Maximum open positions

3–5

Position size

Percentage of account balance.

Example

10% per trade.

Daily loss protection

Stop trading if daily loss exceeds threshold.

Example

3%

Max drawdown protection

If total equity drawdown exceeds threshold → pause bot.

Example

10%

Trade cooldown

Wait period between trades per symbol.

Example

15 minutes.

---

# 10. Paper Trading System

Paper trading must simulate real trading conditions.

Configuration

Paper starting balance

Example

1000 USDT

Simulated fees

0.1%

Optional slippage simulation.

Paper mode must be default.

Live trading must require manual activation.

---

# 11. Dashboard Requirements

A web dashboard must be included.

Accessible via browser.

---

# 12. Dashboard Pages

Overview

Account balance

Daily PnL

Weekly PnL

Win rate

Open positions

Equity curve

Drawdown chart

---

Symbols Page

Shows scanned symbols.

Columns

Symbol

24h Volume

Spread

Trend status

Signal status

---

Trades Page

Open trades

Symbol
Entry price
Current price
PnL
TP
SL
Trade age

Closed trades

Symbol
Entry
Exit
PnL
Exit reason

---

Settings Page

Editable parameters

Max symbols scanned

Max open trades

Minimum volume

Maximum spread

Take profit %

Stop loss %

Trading mode

Paper / Live

Controls

Pause bot

Resume bot

---

Logs Page

Display system logs.

Examples

Market scan started

Signal detected

Trade opened

Trade closed

Risk protection triggered

---

# 13. Telegram Alerts

Send notifications when:

Trade opened

Trade closed

Daily loss limit triggered

System error

Bot paused automatically.

---

# 14. Database Models

Trades

id
symbol
entry_price
exit_price
quantity
status
tp_price
sl_price
entry_time
exit_time
pnl

Symbols

symbol
volume_24h
spread
last_price

Settings

key
value

Logs

timestamp
event_type
symbol
message

---

# 15. Project Architecture

crypto-bot/

app/

main.py

core/

config.py
database.py

services/

binance_client.py
scanner.py
indicators.py
strategy.py
risk.py
executor.py

jobs/

run_cycle.py

models/

trade.py
symbol.py
settings.py
log.py

web/

templates
static

api/

dashboard_routes.py
trade_routes.py
settings_routes.py

requirements.txt
render.yaml
README.md

---

# 16. Trading Cycle

Each cycle performs:

1 Market scan
2 Candidate filtering
3 Strategy evaluation
4 Risk validation
5 Trade execution
6 Position management
7 Logging

Cycle interval

Every 5 minutes.

---

# 17. Deployment on Render

Two services required.

Web Service

Runs FastAPI dashboard.

Command

uvicorn app.main:app --host 0.0.0.0 --port 10000

Cron Job

Runs trading cycle.

Command

python -m app.jobs.run_cycle

Schedule

Every 5 minutes.

---

# 18. Security

Binance API keys must be stored in environment variables.

BINANCE_API_KEY

BINANCE_API_SECRET

Rules

Disable withdrawals

Never log API keys

---

# 19. Acceptance Criteria

The project is considered complete when:

Dashboard loads correctly

Scanner detects markets

Paper trading executes simulated trades

Live trading executes Binance orders

Risk management protections work

Logs capture all events

Telegram alerts function correctly.

---

# 20. Future Improvements

Backtesting system

Multiple strategies

Portfolio allocation

AI signal scoring

Advanced analytics dashboard

---

# END OF DOCUMENT
