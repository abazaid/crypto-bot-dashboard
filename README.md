# Crypto Bot Dashboard

Paper-trading crypto bot with live Binance market data, scanner, strategy engine, risk controls, and dashboard.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Optional Telegram alerts:

```bash
set TELEGRAM_BOT_TOKEN=your_bot_token
set TELEGRAM_CHAT_ID=your_chat_id
```

Open:

`http://127.0.0.1:8000`

## Pages included

- `/` Overview
- `/symbols` Symbols scanner
- `/trades` Open/closed trades
- `/settings` Trading settings
- `/logs` System logs
- `/statistics` performance metrics

## Implemented strategy/risk modules

- BTC market regime filter (`BTC 1H EMA50 < EMA200` blocks new trades)
- Two-layer scanner: liquidity scan + momentum candidate watchlist
- Momentum detection:
  - Volume accumulation
  - Volatility expansion (BB squeeze expansion)
  - Relative strength vs BTC
- Entry confirmation:
  - Trend pullback
  - Volume spike > `1.5x` average (20 candles)
  - Resistance distance filter (>2%)
- Trailing take-profit:
  - Trigger at +2%
  - Trailing stop 0.8%
- Max trade duration, symbol cooldown, daily loss protection
- Smart position sizing (risk-per-trade)
