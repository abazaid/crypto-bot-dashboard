# Crypto Bot Dashboard

Paper-trading crypto bot with live Binance market data, scanner, strategy engine, risk controls, and dashboard.

## Live mirroring mode

- Keep one strategy configuration in `/settings`.
- When `Trading mode = Live`, each approved paper entry is mirrored to Binance Spot (main account) using market orders.
- When the paper trade closes (TP/SL/trailing/time/manual), a matching market close is sent to Binance.
- API keys are read only from environment variables:
  - `BINANCE_API_KEY`
  - `BINANCE_API_SECRET`
- Exit monitoring is decoupled from scanner cycle using `POSITION_WATCH_SECONDS` (default `10s`) for faster SL/TP/trailing reactions.

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
